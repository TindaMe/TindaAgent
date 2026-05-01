from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
from pathlib import Path

import uvicorn

from TindaAgent.Process.Architecture.paths import get_runtime_root

_DEFAULT_PORT = 8000
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT_RETRIES = 20
_PORTS_FILE_NAME = ".tinda_ports.list"
_PORTS_ENV_VAR = "TINDA_ACTIVE_PORTS"


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
    # Query WSL listen state from Windows side using shared tracking file.
    # WSL side writes selected ports to .tinda_ports.list, which is visible from Windows.
    try:
        tracked = _load_tracked_ports()
    except Exception:
        tracked = []
    return int(port) in set(int(x) for x in tracked)


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


def _pick_port_with_retry(host: str, start_port: int, retries: int) -> tuple[int, int]:
    max_retries = max(0, int(retries))
    base = int(start_port)
    for offset in range(max_retries + 1):
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


def _parse_ports_text(text: str) -> list[int]:
    out: list[int] = []
    if not text:
        return out
    normalized = text.replace(",", " ").replace(";", " ").replace("\n", " ").replace("\r", " ")
    for token in normalized.split():
        t = token.strip()
        if not t or t == "\"\"":
            continue
        if not t.isdigit():
            continue
        p = int(t)
        if p <= 0 or p > 65535:
            continue
        if p not in out:
            out.append(p)
    return out


def _load_tracked_ports(file_path: Path | None = None, env_value: str | None = None) -> list[int]:
    fp = file_path or _ports_file_path()
    ports: list[int] = []
    if fp.exists():
        try:
            ports.extend(_parse_ports_text(fp.read_text(encoding="utf-8", errors="ignore")))
        except Exception:
            pass
    if env_value is None:
        env_value = str(os.environ.get(_PORTS_ENV_VAR, ""))
    ports.extend(_parse_ports_text(str(env_value)))
    dedup: list[int] = []
    for p in ports:
        if p not in dedup:
            dedup.append(p)
    return dedup


def _write_tracked_ports_file(ports: list[int], file_path: Path | None = None) -> None:
    fp = file_path or _ports_file_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    lines = [str(int(p)) for p in ports if int(p) > 0 and int(p) <= 65535]
    tmp = fp.with_name(f"{fp.name}.tmp.{os.getpid()}")
    body = ("\n".join(lines) + "\n") if lines else ""
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(fp)


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


def _update_tracked_port(port: int, add: bool, *, file_path: Path | None = None, sync_windows_env: bool = True) -> list[int]:
    # Keep tracking in run_web.py so all launchers (Windows .bat / Linux start.sh / direct python)
    # write the same source of truth, including real port after retry.
    current = _load_tracked_ports(file_path=file_path)
    merged = {int(p) for p in current if int(p) > 0 and int(p) <= 65535}
    p = int(port)
    if p > 0 and p <= 65535:
        if add:
            merged.add(p)
        else:
            merged.discard(p)
    ordered = sorted(merged)
    _write_tracked_ports_file(ordered, file_path=file_path)
    _set_ports_env(ordered, sync_windows_env=sync_windows_env)
    return ordered

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
    parser.add_argument("--reload", action="store_true", help="启用热重载（默认关闭）")
    args = parser.parse_args()

    app_dir = _load_selected_app_dir()
    app_import = _pick_app_import(app_dir)
    selected_port, offset = _pick_port_with_retry(args.host, int(args.port), int(args.port_retries))
    if offset > 0:
        print(f"[start] 端口 {args.port} 已占用，自动切换到 {selected_port}（+{offset}）")
    print(f"[start] 服务地址: http://{_to_local_visit_host(args.host)}:{selected_port}")

    uvicorn_kw = {"host": args.host, "port": selected_port, "reload": bool(args.reload)}
    if app_dir is not None:
        uvicorn_kw["app_dir"] = str(app_dir)
        if bool(args.reload):
            uvicorn_kw["reload_dirs"] = [str(app_dir)]

    tracking_ok = False
    try:
        _update_tracked_port(selected_port, add=True)
        tracking_ok = True
    except Exception as exc:
        print(f"[start] 端口追踪写入失败: {exc}")

    try:
        uvicorn.run(app_import, **uvicorn_kw)
    finally:
        if tracking_ok:
            try:
                _update_tracked_port(selected_port, add=False)
            except Exception as exc:
                print(f"[stop] 端口追踪清理失败: {exc}")
