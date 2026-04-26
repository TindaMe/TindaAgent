from __future__ import annotations

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
    app_dir = _load_selected_app_dir()
    app_import = _pick_app_import(app_dir)
    if app_dir is not None:
        uvicorn.run(
            app_import,
            host="0.0.0.0",
            port=8000,
            reload=True,
            app_dir=str(app_dir),
            reload_dirs=[str(app_dir)],
        )
    else:
        uvicorn.run(app_import, host="0.0.0.0", port=8000, reload=True)
