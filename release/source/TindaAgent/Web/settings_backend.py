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
CONTEXT_TOKEN_LIMIT_MIN = 16000
CONTEXT_TOKEN_LIMIT_MAX_FALLBACK = 200000
CONTEXT_TOKEN_LIMIT_DEFAULT = 16000

DEFAULT_WEB_SETTINGS: dict[str, Any] = {
    "stream_enabled": True,
    "terminal_open": False,
    "token_limit": CONTEXT_TOKEN_LIMIT_DEFAULT,
    "quick_buttons": ["model", "stream", "terminal", "compress"],
    "restore_last_session": False,
    "last_session_id": "",
    "title_model": "deepseek-v4-flash",
    "compress_model": "deepseek-v4-flash",
}


def normalize_context_token_limit(value: Any, *, default: int = CONTEXT_TOKEN_LIMIT_DEFAULT) -> int:
    try:
        parsed = int(value)
    except Exception:
        return int(default)
    if CONTEXT_TOKEN_LIMIT_MIN <= parsed <= CONTEXT_TOKEN_LIMIT_MAX_FALLBACK:
        return int(parsed)
    return int(default)


def validate_context_token_limit(value: Any) -> tuple[bool, int, str]:
    try:
        parsed = int(value)
    except Exception:
        return False, CONTEXT_TOKEN_LIMIT_DEFAULT, (
            f"上下文阈值必须是数字，范围为 {CONTEXT_TOKEN_LIMIT_MIN} ~ {CONTEXT_TOKEN_LIMIT_MAX_FALLBACK}"
        )
    if parsed < CONTEXT_TOKEN_LIMIT_MIN or parsed > CONTEXT_TOKEN_LIMIT_MAX_FALLBACK:
        return False, CONTEXT_TOKEN_LIMIT_DEFAULT, (
            f"上下文阈值范围为 {CONTEXT_TOKEN_LIMIT_MIN} ~ {CONTEXT_TOKEN_LIMIT_MAX_FALLBACK}"
        )
    return True, int(parsed), ""


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
    merged["token_limit"] = normalize_context_token_limit(merged.get("token_limit"))
    return merged


def save_web_settings(data: dict[str, Any]) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = load_web_settings()
    merged.update(data)
    merged["token_limit"] = normalize_context_token_limit(merged.get("token_limit"))
    # Only persist known keys
    clean = {k: merged.get(k, DEFAULT_WEB_SETTINGS[k]) for k in DEFAULT_WEB_SETTINGS}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_context_token_limit() -> int:
    """全局唯一的上下文 token 阈值入口。来源：web-settings.json，默认 16k。"""
    return normalize_context_token_limit(load_web_settings().get("token_limit"))


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
        s = terminal_policy.load_settings()
        bypass = bool(s.get("bypass_terminal_confirm", False))
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
