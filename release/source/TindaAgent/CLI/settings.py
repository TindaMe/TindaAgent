"""CLI 配置缓存：模型、上次会话、偏好设置。"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILE = Path("~/.tinda/agent/cli-settings.json").expanduser()


def load() -> dict:
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_last_session() -> str:
    return str(load().get("last_session", "")).strip()


def set_last_session(sid: str) -> None:
    data = load()
    data["last_session"] = str(sid or "").strip()
    save(data)


def get_model() -> str:
    return str(load().get("model", "")).strip()


def set_model(model: str) -> None:
    data = load()
    data["model"] = str(model or "").strip()
    save(data)
