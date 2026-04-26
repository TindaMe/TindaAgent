from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from TindaAgent.Process.Architecture.paths import ensure_runtime_dirs, get_runtime_root, get_data_root, get_log_root
from TindaAgent.Process.Architecture.versioning import get_app_version
from TindaAgent.Process.Observability import audit_event

_THIS_FILE = str(Path(__file__).resolve())

GITHUB_REPO = "TindaMe/TindaAgent"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

_MANIFEST_FILE = "manifest.json"
_SIG_FILE = "manifest.sig"
_META_FILE = "manifest.meta.json"
_CURRENT_FILE = "current.json"

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_text(v: Any) -> str:
    return str(v if v is not None else "")


def _normalize_version_text(v: str) -> str:
    text = str(v or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    return text


def _semver_key(v: str) -> tuple[int, int, int, str]:
    text = _normalize_version_text(v)
    m = _SEMVER_RE.match(text)
    if not m:
        return (0, 0, 0, text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), text)


def _json_canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copytree_filtered(src: Path, dst: Path, skip_names: set[str] | None = None) -> None:
    if not src.exists():
        return
    if skip_names is None:
        skip_names = set()
    for item in src.iterdir():
        if item.name in skip_names:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _read_version_from_pyproject(project_root: Path) -> str:
    pyproject = project_root / "pyproject.toml"
    try:
        if not pyproject.exists():
            return ""
        text = pyproject.read_text(encoding="utf-8")
        m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if not m:
            return ""
        return _normalize_version_text(m.group(1))
    except Exception:
        return ""


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _http_get_json(url: str, timeout: int = 8) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "TindaAgent-VersionManager/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        text = resp.read().decode(charset, errors="replace")
        return json.loads(text)


def _http_get_bytes(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "TindaAgent-VersionManager/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    signature_id: str
    manifest_sha256: str
    error: str = ""


class VersionManager:
    def __init__(self, runtime_root: Path | None = None) -> None:
        ensure_runtime_dirs()
        self.runtime_root = (runtime_root or get_runtime_root()).resolve()
        self.versions_root = self.runtime_root / "versions"
        self.shared_root = self.runtime_root / "shared"
        self.current_path = self.runtime_root / _CURRENT_FILE
        self.trust_dir = self.runtime_root / "trust"
        self.pubkeys_file = self.trust_dir / "release_pubkeys.json"
        self.migrations_root = self.runtime_root / "migrations"
        self.compat_file = self.migrations_root / "schema.json"
        self.switch_lock = self.runtime_root / "switch.lock"

        self.versions_root.mkdir(parents=True, exist_ok=True)
        self.shared_root.mkdir(parents=True, exist_ok=True)
        self.trust_dir.mkdir(parents=True, exist_ok=True)
        self.migrations_root.mkdir(parents=True, exist_ok=True)

        self._ensure_default_pubkeys()
        self._ensure_schema_state()
        self._ensure_current_state()

    def _ensure_default_pubkeys(self) -> None:
        if self.pubkeys_file.exists():
            return
        payload = {
            "version": 1,
            "keys": [
                {
                    "key_id": "tinda-release-dev",
                    "algorithm": "ed25519",
                    "public_key_b64": "",  # 发布时替换
                    "enabled": False,
                }
            ],
            "updated_at": _now_iso(),
        }
        _write_json_atomic(self.pubkeys_file, payload)

    def _ensure_schema_state(self) -> None:
        if self.compat_file.exists():
            return
        payload = {
            "schema_version": 1,
            "updated_at": _now_iso(),
        }
        _write_json_atomic(self.compat_file, payload)

    def _ensure_current_state(self) -> None:
        if self.current_path.exists():
            return
        app_version = _normalize_version_text(get_app_version())
        payload = {
            "version": app_version,
            "app_path": "",
            "signature_id": "",
            "manifest_sha256": "",
            "verified": False,
            "source": "local",
            "switched_at": _now_iso(),
        }
        _write_json_atomic(self.current_path, payload)

    def get_current(self) -> dict[str, Any]:
        row = _read_json(self.current_path, {})
        if not isinstance(row, dict):
            row = {}
        row.setdefault("version", _normalize_version_text(get_app_version()))
        row.setdefault("app_path", "")
        row.setdefault("signature_id", "")
        row.setdefault("manifest_sha256", "")
        row.setdefault("verified", False)
        row.setdefault("source", "local")
        row.setdefault("switched_at", "")
        return row

    def align_current_to_runtime(self, runtime_version: str, runtime_app_path: str, *, keep_switched_at: bool = True) -> dict[str, Any]:
        """
        当运行代码版本与 current.json 历史指针不一致时，按当前运行时状态对齐 current.json。
        """
        target_version = _normalize_version_text(runtime_version) or _normalize_version_text(get_app_version())
        target_app_path = str(runtime_app_path or "").strip()
        row = self.get_current()
        row["version"] = target_version
        row["app_path"] = target_app_path
        row["source"] = "local"
        row["verified"] = False
        row["signature_id"] = ""
        row["manifest_sha256"] = ""
        if (not keep_switched_at) or (not str(row.get("switched_at", "")).strip()):
            row["switched_at"] = _now_iso()
        _write_json_atomic(self.current_path, row)
        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="versioning",
            func="VersionManager.align_current_to_runtime",
            file_path=_THIS_FILE,
            content=f"align_current_to_runtime version={target_version}",
            extra={"version": target_version, "app_path": target_app_path},
        )
        return row

    def _load_enabled_pubkeys(self) -> list[tuple[str, Ed25519PublicKey]]:
        data = _read_json(self.pubkeys_file, {})
        rows = data.get("keys", []) if isinstance(data, dict) else []
        out: list[tuple[str, Ed25519PublicKey]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            if not bool(item.get("enabled", False)):
                continue
            if str(item.get("algorithm", "")).lower() != "ed25519":
                continue
            key_id = str(item.get("key_id", "")).strip() or "unknown"
            key_b64 = str(item.get("public_key_b64", "")).strip()
            if not key_b64:
                continue
            try:
                raw = base64.b64decode(key_b64)
                pub = Ed25519PublicKey.from_public_bytes(raw)
            except Exception:
                continue
            out.append((key_id, pub))
        return out

    def verify_manifest(self, manifest: dict[str, Any], sig_bytes: bytes) -> VerifyResult:
        manifest_bytes = _json_canonical_bytes(manifest)
        manifest_hash = _sha256_bytes(manifest_bytes)
        signature_id = "sig_" + _sha256_bytes(manifest_bytes + sig_bytes)[:16]
        keys = self._load_enabled_pubkeys()
        if not keys:
            return VerifyResult(False, signature_id, manifest_hash, "no enabled public keys")
        for key_id, pub in keys:
            try:
                pub.verify(sig_bytes, manifest_bytes)
                return VerifyResult(True, signature_id, manifest_hash)
            except InvalidSignature:
                continue
            except Exception:
                continue
        return VerifyResult(False, signature_id, manifest_hash, "signature verify failed")

    def _version_dir(self, version: str) -> Path:
        return self.versions_root / _normalize_version_text(version)

    def list_local_versions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        current = self.get_current()
        cur_ver = _normalize_version_text(str(current.get("version", "")))
        for path in sorted(self.versions_root.iterdir() if self.versions_root.exists() else [], key=lambda p: p.name):
            if not path.is_dir():
                continue
            manifest_path = path / _MANIFEST_FILE
            meta_path = path / _META_FILE
            manifest = _read_json(manifest_path, {}) if manifest_path.exists() else {}
            meta = _read_json(meta_path, {}) if meta_path.exists() else {}
            version = _safe_text(manifest.get("version") or path.name)
            version_norm = _normalize_version_text(version)
            source = str(meta.get("source", "local") or "local")
            verified = bool(meta.get("verified", False))
            signature_id = str(meta.get("signature_id", ""))
            verify_error = str(meta.get("verify_error", ""))
            if not verify_error and source == "local":
                verify_error = "local version has no verified release signature"
            rows.append(
                {
                    "version": version,
                    "dir": str(path),
                    "installed": True,
                    "verified": verified,
                    "signature_id": signature_id,
                    "manifest_sha256": str(meta.get("manifest_sha256", "")),
                    "is_current": version_norm == cur_ver,
                    "source": source,
                    "verify_error": verify_error,
                }
            )
        rows.sort(key=lambda x: _semver_key(str(x.get("version", ""))), reverse=True)
        return rows

    def create_local_snapshot(self, version: str) -> dict[str, Any]:
        target = _normalize_version_text(version)
        if not target:
            return {"ok": False, "error": "version required"}

        version_dir = self._version_dir(target)
        if version_dir.exists():
            return {"ok": False, "error": f"version already exists: {target}"}

        app_dir = version_dir / "app"
        version_dir.mkdir(parents=True, exist_ok=True)
        app_dir.mkdir(parents=True, exist_ok=True)

        src_project = Path(__file__).resolve().parents[3]
        src_project_version = _read_version_from_pyproject(src_project)
        if src_project_version and src_project_version != target:
            return {"ok": False, "error": f"snapshot version mismatch: target={target}, source={src_project_version}"}
        try:
            _copytree_filtered(src_project, app_dir, skip_names={"__pycache__", ".git", ".pytest_cache", ".mypy_cache"})
            shared_data = self.shared_root / "data"
            snapshot_data = version_dir / "data_snapshot"
            if shared_data.exists():
                shutil.copytree(shared_data, snapshot_data, dirs_exist_ok=True)
        except Exception as e:
            try:
                shutil.rmtree(version_dir)
            except Exception:
                pass
            return {"ok": False, "error": f"snapshot copy failed: {e}"}

        manifest_obj = {
            "app": "TindaAgent",
            "version": target,
            "build_time": _now_iso(),
            "release_channel": "local-snapshot",
            "commit": "local-snapshot",
            "archive_sha256": "",
            "data_schema_version": int(_read_json(self.compat_file, {"schema_version": 1}).get("schema_version", 1)),
            "min_compatible_schema": 1,
            "max_compatible_schema": int(_read_json(self.compat_file, {"schema_version": 1}).get("schema_version", 1)),
        }
        manifest_bytes = _json_canonical_bytes(manifest_obj)
        sig_bytes = b"local-snapshot-signature"
        signature_id = "local_" + _sha256_bytes(manifest_bytes + sig_bytes)[:16]
        manifest_sha = _sha256_bytes(manifest_bytes)

        _write_json_atomic(version_dir / _MANIFEST_FILE, manifest_obj)
        (version_dir / _SIG_FILE).write_bytes(sig_bytes)
        _write_json_atomic(
            version_dir / _META_FILE,
            {
                "verified": False,
                "signature_id": signature_id,
                "manifest_sha256": manifest_sha,
                "installed_at": _now_iso(),
                "source": "local_snapshot",
                "verify_error": "local snapshot (unsigned)",
            },
        )

        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="versioning",
            func="VersionManager.create_local_snapshot",
            file_path=_THIS_FILE,
            content=f"create_local_snapshot_done version={target}",
            extra={"version": target, "signature_id": signature_id},
        )
        return {
            "ok": True,
            "version": target,
            "installed": True,
            "verified": False,
            "signature_id": signature_id,
            "manifest_sha256": manifest_sha,
            "source": "local_snapshot",
            "app_path": str(app_dir),
        }

    def create_snapshot_from_current_code(self) -> dict[str, Any]:
        src_project = Path(__file__).resolve().parents[3]
        src_version = _read_version_from_pyproject(src_project)
        if not src_version:
            return {"ok": False, "error": "cannot detect project version from pyproject.toml"}
        version_dir = self._version_dir(src_version)
        if version_dir.exists():
            return {"ok": False, "error": f"version already exists: {src_version}"}
        return self.create_local_snapshot(src_version)

    def _extract_release_assets(self, release_item: dict[str, Any]) -> dict[str, str]:
        assets = release_item.get("assets", []) if isinstance(release_item, dict) else []
        out: dict[str, str] = {}
        for a in assets:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name", "")).strip()
            url = str(a.get("browser_download_url", "")).strip()
            if not name or not url:
                continue
            out[name] = url
        return out

    def list_remote_releases(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "source": "github_releases",
            "repo": GITHUB_REPO,
            "releases": [],
            "latest_verified": None,
            "error": "",
        }
        try:
            payload = _http_get_json(RELEASES_API)
            if not isinstance(payload, list):
                raise ValueError("github releases payload invalid")
        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)
            return result

        rows: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag_name", "")).strip()
            version = _normalize_version_text(tag)
            if not version:
                continue
            assets = self._extract_release_assets(item)
            manifest_url = assets.get(_MANIFEST_FILE, "")
            sig_url = assets.get(_SIG_FILE, "")
            archive_url = ""
            for k, v in assets.items():
                lk = k.lower()
                if lk.endswith(".zip") or lk.endswith(".tar.gz") or lk.endswith(".tgz"):
                    archive_url = v
                    break
            row = {
                "version": version,
                "tag": tag,
                "name": str(item.get("name", "") or ""),
                "published_at": str(item.get("published_at", "") or ""),
                "prerelease": bool(item.get("prerelease", False)),
                "draft": bool(item.get("draft", False)),
                "manifest_url": manifest_url,
                "sig_url": sig_url,
                "archive_url": archive_url,
                "verified": False,
                "signature_id": "",
                "manifest_sha256": "",
                "verify_error": "",
                "source": "github_releases",
            }

            if manifest_url and sig_url:
                try:
                    manifest_obj = _http_get_json(manifest_url)
                    sig_bytes = _http_get_bytes(sig_url)
                    if isinstance(manifest_obj, dict):
                        vr = self.verify_manifest(manifest_obj, sig_bytes)
                        row["verified"] = bool(vr.ok)
                        row["signature_id"] = vr.signature_id
                        row["manifest_sha256"] = vr.manifest_sha256
                        row["verify_error"] = vr.error
                except Exception as e:
                    row["verify_error"] = str(e)
            else:
                row["verify_error"] = "manifest or signature asset missing"
            rows.append(row)

        rows.sort(key=lambda x: _semver_key(str(x.get("version", ""))), reverse=True)
        result["releases"] = rows

        verified_stable = [x for x in rows if x.get("verified") and not x.get("prerelease") and not x.get("draft")]
        if verified_stable:
            result["latest_verified"] = verified_stable[0]
        return result

    def check_target_compat(self, target_version: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        target = _normalize_version_text(target_version)
        schema_state = _read_json(self.compat_file, {"schema_version": 1})
        current_schema = int(schema_state.get("schema_version", 1))

        min_compat = 1
        max_compat = current_schema
        target_schema = current_schema
        if isinstance(manifest, dict) and manifest:
            try:
                min_compat = int(manifest.get("min_compatible_schema", min_compat))
            except Exception:
                pass
            try:
                max_compat = int(manifest.get("max_compatible_schema", max_compat))
            except Exception:
                pass
            try:
                target_schema = int(manifest.get("data_schema_version", target_schema))
            except Exception:
                pass

        ok = (min_compat <= current_schema <= max_compat)
        return {
            "ok": bool(ok),
            "target_version": target,
            "current_schema": current_schema,
            "target_schema": target_schema,
            "min_compatible_schema": min_compat,
            "max_compatible_schema": max_compat,
            "needs_migration": current_schema != target_schema,
            "error": "" if ok else "schema not compatible",
        }

    def _run_schema_migration(self, target_schema: int) -> dict[str, Any]:
        state = _read_json(self.compat_file, {"schema_version": 1})
        current_schema = int(state.get("schema_version", 1))
        if current_schema == target_schema:
            return {"ok": True, "from": current_schema, "to": target_schema, "steps": []}

        # 当前先实现安全骨架：记录迁移并切 schema；后续可插入逐版本真实迁移步骤
        steps = [{"from": current_schema, "to": target_schema, "action": "schema_state_update"}]
        state["schema_version"] = int(target_schema)
        state["updated_at"] = _now_iso()
        _write_json_atomic(self.compat_file, state)
        return {"ok": True, "from": current_schema, "to": target_schema, "steps": steps}

    def _copy_shared_for_backup(self) -> Path:
        self.shared_root.mkdir(parents=True, exist_ok=True)
        bak_root = self.migrations_root / f"backup_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}"
        bak_root.mkdir(parents=True, exist_ok=True)
        # 只备份共享核心数据
        shared_data = self.shared_root / "data"
        if shared_data.exists():
            shutil.copytree(shared_data, bak_root / "data", dirs_exist_ok=True)
        return bak_root

    def _restore_backup(self, backup_root: Path) -> None:
        dst = self.shared_root / "data"
        if dst.exists():
            shutil.rmtree(dst)
        src = backup_root / "data"
        if src.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)

    def install_from_release(self, version: str) -> dict[str, Any]:
        target = _normalize_version_text(version)
        if not target:
            return {"ok": False, "error": "version required"}

        remote = self.list_remote_releases()
        if not remote.get("ok"):
            return {"ok": False, "error": str(remote.get("error") or "remote unavailable")}

        rel = None
        for item in remote.get("releases", []):
            if _normalize_version_text(item.get("version", "")) == target:
                rel = item
                break
        if rel is None:
            return {"ok": False, "error": f"release not found: {target}"}
        if not rel.get("verified"):
            return {"ok": False, "error": f"release not verified: {target}"}

        manifest_url = str(rel.get("manifest_url", ""))
        sig_url = str(rel.get("sig_url", ""))
        archive_url = str(rel.get("archive_url", ""))
        if not (manifest_url and sig_url and archive_url):
            return {"ok": False, "error": "release assets incomplete"}

        try:
            manifest_obj = _http_get_json(manifest_url)
            sig_bytes = _http_get_bytes(sig_url)
            archive_bytes = _http_get_bytes(archive_url)
        except Exception as e:
            return {"ok": False, "error": f"download failed: {e}"}

        if not isinstance(manifest_obj, dict):
            return {"ok": False, "error": "manifest invalid"}

        vr = self.verify_manifest(manifest_obj, sig_bytes)
        if not vr.ok:
            return {"ok": False, "error": f"manifest verify failed: {vr.error}"}

        manifest_sha = vr.manifest_sha256
        sig_id = vr.signature_id

        archive_sha = _sha256_bytes(archive_bytes)
        expected_archive_sha = str(manifest_obj.get("archive_sha256", "")).strip().lower()
        if expected_archive_sha and expected_archive_sha != archive_sha:
            return {"ok": False, "error": "archive sha256 mismatch"}

        version_dir = self._version_dir(target)
        app_dir = version_dir / "app"
        version_dir.mkdir(parents=True, exist_ok=True)

        archive_path = version_dir / "package.bin"
        archive_path.write_bytes(archive_bytes)

        # 最小实现：保存安装包，后续可扩展自动解压。当前 app_path 指向版本目录 app。
        app_dir.mkdir(parents=True, exist_ok=True)

        _write_json_atomic(version_dir / _MANIFEST_FILE, manifest_obj)
        (version_dir / _SIG_FILE).write_bytes(sig_bytes)
        _write_json_atomic(
            version_dir / _META_FILE,
            {
                "verified": True,
                "signature_id": sig_id,
                "manifest_sha256": manifest_sha,
                "archive_sha256": archive_sha,
                "installed_at": _now_iso(),
                "source": "github_releases",
                "archive_file": str(archive_path.name),
            },
        )

        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="versioning",
            func="VersionManager.install_from_release",
            file_path=_THIS_FILE,
            content=f"install_version_done version={target}",
            extra={"version": target, "signature_id": sig_id, "manifest_sha256": manifest_sha},
        )

        return {
            "ok": True,
            "version": target,
            "installed": True,
            "verified": True,
            "signature_id": sig_id,
            "manifest_sha256": manifest_sha,
            "app_path": str(app_dir),
        }

    def switch_version(self, version: str) -> dict[str, Any]:
        target = _normalize_version_text(version)
        if not target:
            return {"ok": False, "error": "version required"}

        version_dir = self._version_dir(target)
        manifest_path = version_dir / _MANIFEST_FILE
        sig_path = version_dir / _SIG_FILE
        meta_path = version_dir / _META_FILE
        app_dir = version_dir / "app"

        if not version_dir.exists() or not manifest_path.exists() or not sig_path.exists() or not meta_path.exists():
            return {"ok": False, "error": f"version not installed: {target}"}
        server_entry = app_dir / "TindaAgent" / "Web" / "server.py"
        legacy_server_entry = app_dir / "Web" / "server.py"
        if not server_entry.exists() and not legacy_server_entry.exists():
            return {"ok": False, "error": f"version package incomplete: missing {server_entry} or {legacy_server_entry}"}

        manifest = _read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            return {"ok": False, "error": "manifest invalid"}
        meta = _read_json(meta_path, {})
        source = str(meta.get("source", "local") or "local")
        sig_bytes = sig_path.read_bytes()
        vr = self.verify_manifest(manifest, sig_bytes)
        local_source = source in {"local", "local_snapshot"}
        if not vr.ok and not local_source:
            return {"ok": False, "error": f"manifest verify failed: {vr.error}"}

        effective_signature_id = vr.signature_id if vr.ok else str(meta.get("signature_id", ""))
        effective_manifest_sha = vr.manifest_sha256 if vr.ok else str(meta.get("manifest_sha256", ""))
        effective_verified = bool(vr.ok)

        compat = self.check_target_compat(target, manifest)
        if not compat.get("ok"):
            return {"ok": False, "error": str(compat.get("error") or "compat failed"), "compat": compat}

        # 自动迁移 + 失败回滚
        backup = self._copy_shared_for_backup()
        old_current = self.get_current()

        try:
            if compat.get("needs_migration"):
                mig = self._run_schema_migration(int(compat.get("target_schema", compat.get("current_schema", 1))))
                if not mig.get("ok"):
                    raise RuntimeError("migration failed")

            current_payload = {
                "version": target,
                "app_path": str(app_dir),
                "signature_id": effective_signature_id,
                "manifest_sha256": effective_manifest_sha,
                "verified": effective_verified,
                "source": source,
                "switched_at": _now_iso(),
            }
            _write_json_atomic(self.current_path, current_payload)
        except Exception as e:
            # 回滚共享数据和 current
            try:
                self._restore_backup(backup)
            except Exception:
                pass
            try:
                _write_json_atomic(self.current_path, old_current)
            except Exception:
                pass
            return {"ok": False, "error": f"switch failed and rolled back: {e}"}

        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="versioning",
            func="VersionManager.switch_version",
            file_path=_THIS_FILE,
            content=f"switch_version_done version={target}",
            extra={"version": target, "signature_id": effective_signature_id, "source": source, "verified": effective_verified},
        )

        return {
            "ok": True,
            "version": target,
            "signature_id": effective_signature_id,
            "manifest_sha256": effective_manifest_sha,
            "verified": effective_verified,
            "source": source,
            "requires_restart": True,
            "current": self.get_current(),
        }


def get_version_manager() -> VersionManager:
    return VersionManager()
