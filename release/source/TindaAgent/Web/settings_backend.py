"""
Web settings persistence module.

Reads/writes ~/.tinda/agent/web-settings.json alongside CLI's cli-settings.json.
Also wraps terminal_policy.py for HTTP endpoint consumption.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from TindaAgent.Process.Architecture.paths import get_runtime_root
from TindaAgent.Process.Security import terminal_policy

SETTINGS_FILE_NAME = "web-settings.json"

DEFAULT_WEB_SETTINGS: dict[str, Any] = {
    "stream_enabled": True,
    "terminal_open": False,
    "token_limit": 16000,
    "quick_buttons": ["model", "stream", "terminal", "compress"],
    "restore_last_session": False,
    "last_session_id": "",
}


def _settings_path() -> Path:
    return get_runtime_root() / SETTINGS_FILE_NAME


def load_web_settings() -> dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        return dict(DEFAULT_WEB_SETTINGS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_WEB_SETTINGS)
    if not isinstance(raw, dict):
        return dict(DEFAULT_WEB_SETTINGS)
    # Merge with defaults so new keys always exist
    merged = dict(DEFAULT_WEB_SETTINGS)
    merged.update(raw)
    return merged


def save_web_settings(data: dict[str, Any]) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = load_web_settings()
    merged.update(data)
    # Only persist known keys
    clean = {k: merged.get(k, DEFAULT_WEB_SETTINGS[k]) for k in DEFAULT_WEB_SETTINGS}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_restore_last_session() -> bool:
    return bool(load_web_settings().get("restore_last_session", False))


def get_last_session_id() -> str:
    return str(load_web_settings().get("last_session_id", "") or "").strip()


def set_last_session_id(sid: str) -> None:
    save_web_settings({"last_session_id": str(sid or "").strip()})


def load_terminal_settings() -> dict[str, Any]:
    """Return terminal settings in a frontend-friendly format."""
    try:
        wl = terminal_policy.get_whitelist()
    except Exception:
        wl = []
    try:
        bl = terminal_policy.get_blacklist()
    except Exception:
        bl = []
    try:
        bypass = terminal_policy.is_bypass_enabled(None)  # reads from disk regardless of user
    except Exception:
        bypass = False
    return {
        "ok": True,
        "whitelist": wl if isinstance(wl, list) else [],
        "blacklist": bl if isinstance(bl, list) else [],
        "bypass_terminal_confirm": bool(bypass),
    }


def save_terminal_settings(whitelist: list[str] | None = None,
                           blacklist: list[str] | None = None,
                           bypass_terminal_confirm: bool | None = None) -> dict[str, Any]:
    """Save terminal settings and return the updated state."""
    try:
        current = terminal_policy.load_settings()
    except Exception:
        current = {}
    if whitelist is not None:
        current["whitelist"] = [str(x).strip() for x in whitelist if str(x).strip()]
    if blacklist is not None:
        current["blacklist"] = [str(x).strip() for x in blacklist if str(x).strip()]
    if bypass_terminal_confirm is not None:
        current["bypass_terminal_confirm"] = bool(bypass_terminal_confirm)
    try:
        terminal_policy.save_settings(current)
        return load_terminal_settings()
    except Exception as e:
        return {"ok": False, "error": str(e)}
