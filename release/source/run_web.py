from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import importlib.util
from pathlib import Path

import uvicorn

from TindaAgent.Process.Architecture.paths import get_runtime_root

_DEFAULT_PORT = 8000
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT_RETRIES = 20
_DEFAULT_FIRST_PORT_WAIT_MS = 1800
_DEFAULT_FIRST_PORT_POLL_MS = 120
_PORTS_FILE_NAME = ".tinda_ports.list"
_PORTS_ENV_VAR = "TINDA_ACTIVE_PORTS"


class _UvicornShutdownNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        if not exc_info:
            return True
        exc_type = exc_info[0]
        if exc_type in {KeyboardInterrupt, asyncio.CancelledError}:
            return False
        return True


def _uvicorn_log_config() -> dict:
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config.setdefault("filters", {})["shutdown_noise"] = {
        "()": f"{__name__}._UvicornShutdownNoiseFilter",
    }
    for handler in config.get("handlers", {}).values():
        filters = handler.setdefault("filters", [])
        if "shutdown_noise" not in filters:
            filters.append("shutdown_noise")
    return config


def _has_watchfiles() -> bool:
    return importlib.util.find_spec("watchfiles") is not None


def _is_wsl_mount_path(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        text = str(path.resolve())
    except Exception:
        text = str(path)
    return _is_wsl() and text.startswith("/mnt/")


def _enable_watchfiles_polling_for_wsl_mounts(app_dir: Path | None) -> bool:
    if not _is_wsl_mount_path(app_dir):
        return False
    # Windows-mounted drives under WSL often do not emit reliable inotify events.
    # Force polling so uvicorn --reload also sees html/js/css/json edits.
    os.environ["WATCHFILES_FORCE_POLLING"] = "true"
    os.environ.setdefault("WATCHFILES_POLL_DELAY_MS", "300")
    return True


def _load_selected_app_dir() -> Path | None:
    runtime_root = get_runtime_root()
    current_file = runtime_root / "current.json"
    if not current_file.exists():
        return None
    try:
        row = json.loads(current_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    app_path = str(row.get("app_path", "")).strip()
    version = str(row.get("version", "")).strip().lstrip("v")
    candidates: list[Path] = []
    if app_path:
        candidates.append(Path(app_path))
    if version:
        candidates.append(runtime_root / "versions" / version / "app")
    for cand in candidates:
        try:
            if (cand / "TindaAgent" / "Web" / "server.py").exists():
                return cand.resolve()
            if (cand / "Web" / "server.py").exists():
                return cand.resolve()
        except Exception:
            continue
    return None


def _pick_app_import(app_dir: Path | None) -> str:
    if app_dir is None:
        return "TindaAgent.Web.server:app"
    if (app_dir / "TindaAgent" / "Web" / "server.py").exists():
        return "TindaAgent.Web.server:app"
    if (app_dir / "Web" / "server.py").exists():
        return "Web.server:app"
    return "TindaAgent.Web.server:app"


def _run_exit_code(cmd: list[str], timeout_sec: float = 3.0) -> int | None:
    try:
        cp = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_sec,
        )
        return int(cp.returncode)
    except Exception:
        return None


def _is_windows() -> bool:
    return os.name == "nt"


def _run_text(cmd: list[str], timeout_sec: float = 6.0) -> tuple[int | None, str]:
    try:
        cp = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_sec,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return int(cp.returncode), str(cp.stdout or "")
    except Exception:
        return None, ""


def _is_port_bindable_local(host: str, port: int) -> bool:
    if port <= 0 or port > 65535:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _is_port_in_use_on_windows_side(port: int) -> bool:
    if port <= 0 or port > 65535:
        return False
    # Query Windows listen state from WSL/Linux side.
    # Prefer netstat parsing because it is much faster than loading PowerShell modules.
    rc, out = _run_text(["cmd.exe", "/c", "netstat -ano -p tcp"], timeout_sec=4.0)
    if rc is not None and out:
        pat = re.compile(rf"^\s*TCP\s+\S+:{int(port)}\s+\S+\s+LISTENING\s+\d+\s*$", re.IGNORECASE)
        for line in out.splitlines():
            if pat.search(line):
                return True
        return False

    script = (
        f"$p={int(port)};"
        "$c=Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue;"
        "if($c){exit 23}else{exit 0}"
    )
    for exe in ("powershell.exe", "pwsh.exe"):
        rc = _run_exit_code([exe, "-NoProfile", "-NonInteractive", "-Command", script], timeout_sec=12.0)
        if rc is None:
            continue
        return rc == 23
    return False


def _is_port_in_use_on_wsl_side(port: int) -> bool:
    if port <= 0 or port > 65535:
        return False
    # Query WSL listen state from Windows side using tracked-port records.
    # When records are env-scoped, only WSL/Linux records are considered cross-env busy.
    try:
        records = _load_tracked_port_records()
    except Exception:
        records = []
    target = int(port)
    for env_tag, p in records:
        if int(p) != target:
            continue
        tag = _normalize_env_tag(env_tag)
        if tag in {"legacy", "wsl", "linux"}:
            return True
    return False


def _is_port_in_use_cross_env(port: int) -> bool:
    if _is_wsl():
        return _is_port_in_use_on_windows_side(port)
    if _is_windows():
        return _is_port_in_use_on_wsl_side(port)
    return False


def _is_port_bindable(host: str, port: int) -> bool:
    if not _is_port_bindable_local(host, port):
        return False
    # Cross-env guard:
    # WSL and Windows use different network stacks, so local bind checks alone can miss
    # "same-port already listening on the other side" and make retry behavior look random.
    if _is_port_in_use_cross_env(port):
        return False
    return True


def _pick_port_with_retry(
    host: str,
    start_port: int,
    retries: int,
    *,
    first_port_wait_ms: int = _DEFAULT_FIRST_PORT_WAIT_MS,
    first_port_poll_ms: int = _DEFAULT_FIRST_PORT_POLL_MS,
) -> tuple[int, int]:
    max_retries = max(0, int(retries))
    base = int(start_port)

    if 0 < base <= 65535:
        if _is_port_bindable(host, base):
            return base, 0

        # Ctrl+C 之后端口释放可能有短暂延迟（尤其是 WSL/Windows 端口转发层）。
        # 先给起始端口一个小等待窗口，避免“第一次重启总跳到 +1”。
        wait_ms = max(0, int(first_port_wait_ms))
        poll_ms = max(10, int(first_port_poll_ms))
        if wait_ms > 0:
            deadline = time.monotonic() + (wait_ms / 1000.0)
            while time.monotonic() < deadline:
                sleep_sec = min(poll_ms / 1000.0, max(0.0, deadline - time.monotonic()))
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
                if _is_port_bindable(host, base):
                    return base, 0

    for offset in range(1, max_retries + 1):
        candidate = base + offset
        if candidate <= 0 or candidate > 65535:
            break
        if _is_port_bindable(host, candidate):
            return candidate, offset
    raise RuntimeError(
        f"未找到可用端口：start={base}, retries={max_retries}（最后尝试到 {base + max_retries}）"
    )


def _to_local_visit_host(host: str) -> str:
    h = str(host or "").strip()
    if h in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return h or "127.0.0.1"


def _ports_file_path() -> Path:
    return Path(__file__).resolve().parent / _PORTS_FILE_NAME


def _is_wsl() -> bool:
    if str(os.environ.get("WSL_DISTRO_NAME", "")).strip():
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except Exception:
        return False


def _current_env_tag() -> str:
    if _is_windows():
        return "windows"
    if _is_wsl():
        return "wsl"
    return "linux"


def _normalize_env_tag(raw: str) -> str:
    tag = str(raw or "").strip().lower()
    if tag in {"", "legacy"}:
        return "legacy"
    if tag in {"win", "windows", "nt"}:
        return "windows"
    if tag in {"wsl"}:
        return "wsl"
    if tag in {"linux", "gnu/linux"}:
        return "linux"
    return tag


def _get_wsl_ip() -> str:
    try:
        cp = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            m = re.search(r"dev\s+(\S+)", cp.stdout)
            if m:
                iface = m.group(1)
                cp2 = subprocess.run(
                    ["ip", "-4", "-o", "addr", "show", iface],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if cp2.returncode == 0:
                    m2 = re.search(r"inet\s+([0-9.]+)", cp2.stdout)
                    if m2:
                        return m2.group(1)
    except Exception:
        pass
    try:
        cp = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "eth0"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if cp.returncode == 0:
            m = re.search(r"inet\s+([0-9.]+)", cp.stdout)
            if m:
                return m.group(1)
    except Exception:
        pass
    try:
        cp = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if cp.returncode == 0:
            for token in (cp.stdout or "").strip().split():
                t = token.strip()
                if t and not t.startswith("127.") and not t.startswith("::1"):
                    return t
    except Exception:
        pass
    return ""


def _parse_port_records_text(text: str, *, default_env: str = "legacy") -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if not text:
        return out
    normalized = text.replace(",", " ").replace(";", " ").replace("\n", " ").replace("\r", " ")
    for token in normalized.split():
        t = token.strip()
        if not t or t == "\"\"":
            continue
        env_tag = default_env
        raw_port = t
        if ":" in t:
            head, tail = t.split(":", 1)
            if head and tail:
                env_tag = _normalize_env_tag(head)
                raw_port = tail
        if not raw_port.isdigit():
            continue
        p = int(raw_port)
        if p <= 0 or p > 65535:
            continue
        item = (_normalize_env_tag(env_tag), p)
        if item not in out:
            out.append(item)
    return out


def _parse_ports_text(text: str) -> list[int]:
    out: list[int] = []
    for _, p in _parse_port_records_text(text, default_env="legacy"):
        if p not in out:
            out.append(p)
    return out


def _load_tracked_port_records(
    file_path: Path | None = None,
    env_value: str | None = None,
    env_tag: str | None = None,
) -> list[tuple[str, int]]:
    fp = file_path or _ports_file_path()
    current_env = _normalize_env_tag(env_tag or _current_env_tag())
    rows: list[tuple[str, int]] = []
    if fp.exists():
        try:
            rows.extend(_parse_port_records_text(fp.read_text(encoding="utf-8", errors="ignore"), default_env="legacy"))
        except Exception:
            pass
    if env_value is None:
        env_value = str(os.environ.get(_PORTS_ENV_VAR, ""))
    rows.extend(_parse_port_records_text(str(env_value), default_env=current_env))
    dedup: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for env_key, p in rows:
        key = (_normalize_env_tag(env_key), int(p))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(key)
    return dedup


def _load_tracked_ports(
    file_path: Path | None = None,
    env_value: str | None = None,
    *,
    env_tag: str | None = None,
    include_foreign: bool = True,
    include_legacy: bool = True,
) -> list[int]:
    fp = file_path or _ports_file_path()
    current_env = _normalize_env_tag(env_tag or _current_env_tag())
    rows = _load_tracked_port_records(file_path=fp, env_value=env_value, env_tag=current_env)
    ports: list[int] = []
    for env_key, p in rows:
        tag = _normalize_env_tag(env_key)
        if tag == "legacy" and not include_legacy:
            continue
        if tag != "legacy" and not include_foreign and tag != current_env:
            continue
        if p not in ports:
            ports.append(p)
    return ports


def _write_tracked_port_records_file(records: list[tuple[str, int]], file_path: Path | None = None) -> None:
    fp = file_path or _ports_file_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for env_key, p in records:
        port = int(p)
        if port <= 0 or port > 65535:
            continue
        tag = _normalize_env_tag(env_key)
        if tag == "legacy":
            lines.append(str(port))
        else:
            lines.append(f"{tag}:{port}")
    tmp = fp.with_name(f"{fp.name}.tmp.{os.getpid()}")
    body = ("\n".join(lines) + "\n") if lines else ""
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(fp)


def _write_tracked_ports_file(ports: list[int], file_path: Path | None = None, *, env_tag: str | None = None) -> None:
    tag = _normalize_env_tag(env_tag or _current_env_tag())
    rows: list[tuple[str, int]] = []
    for p in ports:
        port = int(p)
        if port <= 0 or port > 65535:
            continue
        rows.append((tag, port))
    _write_tracked_port_records_file(rows, file_path=file_path)


def _sync_windows_ports_env(value: str) -> None:
    payload = value if value else "\"\""
    cmd: list[str]
    if os.name == "nt":
        cmd = ["setx", _PORTS_ENV_VAR, payload]
    elif _is_wsl():
        cmd = ["cmd.exe", "/c", "setx", _PORTS_ENV_VAR, payload]
    else:
        return
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
    except Exception:
        pass


def _set_ports_env(ports: list[int], sync_windows_env: bool = True) -> None:
    value = " ".join(str(int(p)) for p in ports if int(p) > 0 and int(p) <= 65535)
    os.environ[_PORTS_ENV_VAR] = value
    if sync_windows_env:
        _sync_windows_ports_env(value)


def _update_tracked_port(
    port: int,
    add: bool,
    *,
    file_path: Path | None = None,
    sync_windows_env: bool = True,
    env_tag: str | None = None,
) -> list[int]:
    # Keep tracking in run_web.py so all launchers (Windows .bat / Linux start.sh / direct python)
    # write the same source of truth, including real port after retry.
    current_env = _normalize_env_tag(env_tag or _current_env_tag())
    current = _load_tracked_port_records(file_path=file_path, env_tag=current_env)
    merged = {
        (_normalize_env_tag(env_key), int(p))
        for env_key, p in current
        if int(p) > 0 and int(p) <= 65535
    }
    p = int(port)
    if p > 0 and p <= 65535:
        scoped = (current_env, p)
        if add:
            merged.add(scoped)
        else:
            merged.discard(scoped)
            # Backward-compat cleanup: remove legacy unscoped port when stopping.
            merged.discard(("legacy", p))
    ordered = sorted(merged, key=lambda x: (x[0], x[1]))
    _write_tracked_port_records_file(ordered, file_path=file_path)
    local_ports = sorted({int(pp) for env_key, pp in ordered if _normalize_env_tag(env_key) == current_env})
    _set_ports_env(local_ports, sync_windows_env=sync_windows_env)
    return local_ports

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TindaAgent 启动器")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT, help=f"监听端口 (默认 {_DEFAULT_PORT})")
    parser.add_argument("--host", type=str, default=_DEFAULT_HOST, help=f"监听地址 (默认 {_DEFAULT_HOST})")
    parser.add_argument(
        "--port-retries",
        type=int,
        default=_DEFAULT_PORT_RETRIES,
        help=f"端口占用时最多顺延重试次数（每次 +1，默认 {_DEFAULT_PORT_RETRIES}）",
    )
    parser.add_argument(
        "--first-port-wait-ms",
        type=int,
        default=_DEFAULT_FIRST_PORT_WAIT_MS,
        help=f"起始端口不可用时的等待窗口（毫秒，默认 {_DEFAULT_FIRST_PORT_WAIT_MS}）",
    )
    parser.add_argument("--reload", action="store_true", help="启用热重载（默认关闭）")
    parser.add_argument("--no-browser", action="store_true", help="禁用自动打开浏览器（默认会自动打开）")
    args = parser.parse_args()

    app_dir = _load_selected_app_dir()
    app_import = _pick_app_import(app_dir)
    selected_port, offset = _pick_port_with_retry(
        args.host,
        int(args.port),
        int(args.port_retries),
        first_port_wait_ms=int(args.first_port_wait_ms),
    )
    if offset > 0:
        print(f"[start] 端口 {args.port} 已占用，自动切换到 {selected_port}（+{offset}）")

    visit_host = _to_local_visit_host(args.host)

    if _is_wsl():
        wsl_ip = _get_wsl_ip()
        if wsl_ip:
            visit_url = f"http://{wsl_ip}:{selected_port}"
            print(f"[start] 服务地址: {visit_url}")
            if wsl_ip != visit_host:
                print(f"[start] 备用: http://{visit_host}:{selected_port} (若 WSL localhost 转发生效)")
        else:
            visit_url = f"http://{visit_host}:{selected_port}"
            print(f"[start] 服务地址: {visit_url}")
    else:
        visit_url = f"http://{visit_host}:{selected_port}"
        print(f"[start] 服务地址: {visit_url}")

    if str(args.host).strip() in {"0.0.0.0", "::"}:
        print("[start] 提示: 0.0.0.0 仅用于监听，浏览器请访问上面的服务地址")

    reload_has_watchfiles = _has_watchfiles()
    reload_uses_polling = False
    if bool(args.reload):
        reload_uses_polling = _enable_watchfiles_polling_for_wsl_mounts(app_dir)
        if reload_has_watchfiles:
            watched = "*.py, *.html, *.js, *.css, *.json"
            polling = "polling" if reload_uses_polling else "native"
            print(f"[start] reload: watchfiles={polling}, includes={watched}")
        else:
            print("[start] reload: python files only (install watchfiles to include html/js/css/json)")

    uvicorn_kw = {
        "host": args.host,
        "port": selected_port,
        "reload": bool(args.reload),
        "log_config": _uvicorn_log_config(),
    }
    if app_dir is not None:
        uvicorn_kw["app_dir"] = str(app_dir)
        if bool(args.reload):
            uvicorn_kw["reload_dirs"] = [str(app_dir)]
            if reload_has_watchfiles:
                uvicorn_kw["reload_includes"] = ["*.py", "*.html", "*.js", "*.css", "*.json"]

    tracking_ok = False
    try:
        _update_tracked_port(selected_port, add=True)
        tracking_ok = True
    except Exception as exc:
        print(f"[start] 端口追踪写入失败: {exc}")

    if not args.no_browser:
        def _open_browser(url: str, local_url: str) -> None:
            import time as _time
            _time.sleep(3.0)

            local_ok = False
            for _ in range(30):
                try:
                    import urllib.request as _ur
                    req = _ur.Request(local_url, method="GET")
                    with _ur.urlopen(req, timeout=2.0) as resp:
                        if 200 <= int(getattr(resp, "status", 0) or 0) < 500:
                            local_ok = True
                            break
                except Exception:
                    _time.sleep(0.5)

            if not local_ok:
                print("[start] 警告: WSL 内本地自检超时，服务可能尚未完全就绪")
                return

            print("[start] 本地自检通过，正在打开浏览器...")
            try:
                if _is_wsl():
                    subprocess.run(
                        ["cmd.exe", "/c", "start", "", url],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                elif os.name == "nt":
                    os.startfile(url)
                else:
                    for opener in ("xdg-open", "sensible-browser", "firefox", "google-chrome"):
                        r = subprocess.run(
                            [opener, url],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                        )
                        if r.returncode == 0:
                            break
            except Exception:
                pass

        threading.Thread(
            target=_open_browser,
            args=(visit_url, f"http://{visit_host}:{selected_port}"),
            daemon=True,
            name="browser-opener",
        ).start()

    try:
        uvicorn.run(app_import, **uvicorn_kw)
    finally:
        if tracking_ok:
            try:
                _update_tracked_port(selected_port, add=False)
            except Exception as exc:
                print(f"[stop] 端口追踪清理失败: {exc}")
