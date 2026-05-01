from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from TindaAgent.Process.Architecture.paths import get_runtime_root


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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TindaAgent 启动器")
    parser.add_argument("--port", type=int, default=8000, help="监听端口 (默认 8000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--no-reload", action="store_true", help="禁用热重载")
    args = parser.parse_args()

    app_dir = _load_selected_app_dir()
    app_import = _pick_app_import(app_dir)
    uvicorn_kw = {"host": args.host, "port": args.port, "reload": not args.no_reload}
    if app_dir is not None:
        uvicorn_kw["app_dir"] = str(app_dir)
        uvicorn_kw["reload_dirs"] = [str(app_dir)]
    uvicorn.run(app_import, **uvicorn_kw)
