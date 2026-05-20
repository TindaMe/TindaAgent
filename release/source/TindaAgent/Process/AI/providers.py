from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from TindaAgent.Process.Architecture.paths import get_system_root

_CONFIG_FILE = get_system_root() / "llm_providers.json"
_LOCK = threading.Lock()
MAX_TOOL_STEPS_LIMIT = 900

_DEFAULT_MODELS = [
    {"id": "deepseek-chat", "label": "deepseek-chat"},
    {"id": "deepseek-v4-pro", "label": "deepseek-pro"},
    {"id": "deepseek-reasoner", "label": "deepseek-reasoner"},
    {"id": "deepseek-v4-flash", "label": "deepseek-v4-flash"},
]

_DEFAULT_CONFIG: dict[str, Any] = {
    "current_provider": "deepseek",
    "providers": {
        "deepseek": {
            "key": "deepseek",
            "label": "DPSK",
            "name": "DeepSeek",
            "adapter": "openai_compatible",
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "chat_path": "/chat/completions",
            "balance_path": "/user/balance",
            "api_key_env": "DEEPSEEK_API_KEY",
            "default_model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "current_model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "enabled": True,
            "builtin": True,
            "balance_supported": True,
            "request_log_supported": True,
            "tool_call_supported": True,
            "tokenizer": "deepseek_official",
            "temperature": 0.7,
            "top_p": None,
            "presence_penalty": None,
            "frequency_penalty": None,
            "max_tokens": None,
            "seed": None,
            "timeout": 25,
            "tool_choice": "auto",
            "max_tool_steps": 6,
            "thinking_enabled": True,
            "thinking_type": "enabled",
            "reasoning_effort": "max",
            "extra_body": {"thinking": {"type": "enabled"}},
            "models": deepcopy(_DEFAULT_MODELS),
        },
        "openai": {
            "key": "openai",
            "label": "OpenAI",
            "name": "OpenAI",
            "adapter": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "chat_path": "/chat/completions",
            "api_key_env": "OPENAI_API_KEY",
            "default_model": "",
            "current_model": "",
            "enabled": True,
            "builtin": True,
            "balance_supported": False,
            "request_log_supported": True,
            "tool_call_supported": True,
            "tokenizer": "provider",
            "temperature": 0.7,
            "top_p": None,
            "presence_penalty": None,
            "frequency_penalty": None,
            "max_tokens": None,
            "seed": None,
            "timeout": 25,
            "tool_choice": "auto",
            "max_tool_steps": 6,
            "thinking_enabled": False,
            "thinking_type": "",
            "reasoning_effort": "",
            "extra_body": {},
            "models": [],
        },
        "google": {
            "key": "google",
            "label": "Google",
            "name": "Google Gemini",
            "adapter": "google_generate_content",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "chat_path": "/models/{model}:generateContent",
            "api_key_env": "GEMINI_API_KEY",
            "api_key_env_fallback": "GOOGLE_API_KEY",
            "default_model": "",
            "current_model": "",
            "enabled": True,
            "builtin": True,
            "balance_supported": False,
            "request_log_supported": True,
            "tool_call_supported": True,
            "tokenizer": "provider",
            "temperature": 0.7,
            "top_p": None,
            "presence_penalty": None,
            "frequency_penalty": None,
            "max_tokens": None,
            "seed": None,
            "timeout": 25,
            "tool_choice": "auto",
            "max_tool_steps": 6,
            "thinking_enabled": False,
            "thinking_type": "",
            "reasoning_effort": "",
            "extra_body": {},
            "models": [],
        },
        "anthropic": {
            "key": "anthropic",
            "label": "Anthropic",
            "name": "Anthropic Claude",
            "adapter": "anthropic_messages",
            "base_url": "https://api.anthropic.com",
            "chat_path": "/v1/messages",
            "api_key_env": "ANTHROPIC_API_KEY",
            "anthropic_version": "2023-06-01",
            "default_model": "",
            "current_model": "",
            "enabled": True,
            "builtin": True,
            "balance_supported": False,
            "request_log_supported": True,
            "tool_call_supported": True,
            "tokenizer": "provider",
            "temperature": 0.7,
            "top_p": None,
            "presence_penalty": None,
            "frequency_penalty": None,
            "max_tokens": None,
            "seed": None,
            "timeout": 25,
            "tool_choice": "auto",
            "max_tool_steps": 6,
            "thinking_enabled": False,
            "thinking_type": "",
            "reasoning_effort": "",
            "extra_body": {},
            "models": [],
        },
    },
}

_SUPPORTED_ADAPTERS = {
    "openai_compatible",
    "google_generate_content",
    "anthropic_messages",
}
_SUPPORTED_TOOL_CHOICES = {"auto", "none", "required"}
_SUPPORTED_REASONING_EFFORTS = {"", "low", "medium", "high", "max"}


def _safe_float(value: object, default: float | None = None, *,
                min_value: float | None = None, max_value: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except Exception:
        return default
    if min_value is not None and parsed < min_value:
        return default
    if max_value is not None and parsed > max_value:
        return default
    return parsed


def _safe_int(value: object, default: int | None = None, *,
              min_value: int | None = None, max_value: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except Exception:
        return default
    if min_value is not None and parsed < min_value:
        return default
    if max_value is not None and parsed > max_value:
        return default
    return parsed


def _safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _normalize_key(value: object, *, fallback: str = "") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-_")
    return text or fallback


def _safe_str(value: object, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _normalize_request_params(provider: dict[str, Any], default: dict[str, Any]) -> None:
    provider["temperature"] = _safe_float(
        provider.get("temperature"),
        _safe_float(default.get("temperature"), 0.7, min_value=0.0, max_value=2.0),
        min_value=0.0,
        max_value=2.0,
    )
    provider["top_p"] = _safe_float(provider.get("top_p"), default.get("top_p"), min_value=0.0, max_value=1.0)
    provider["presence_penalty"] = _safe_float(provider.get("presence_penalty"), default.get("presence_penalty"),
                                               min_value=-2.0, max_value=2.0)
    provider["frequency_penalty"] = _safe_float(provider.get("frequency_penalty"), default.get("frequency_penalty"),
                                                min_value=-2.0, max_value=2.0)
    provider["max_tokens"] = _safe_int(provider.get("max_tokens"), default.get("max_tokens"),
                                       min_value=1, max_value=200000)
    provider["seed"] = _safe_int(provider.get("seed"), default.get("seed"), min_value=0, max_value=4294967295)
    provider["timeout"] = _safe_int(provider.get("timeout"), default.get("timeout", 25),
                                    min_value=1, max_value=600)
    provider["max_tool_steps"] = _safe_int(provider.get("max_tool_steps"), default.get("max_tool_steps", 6),
                                           min_value=1, max_value=MAX_TOOL_STEPS_LIMIT)

    tool_choice = _safe_str(provider.get("tool_choice"), _safe_str(default.get("tool_choice"), "auto")).lower()
    provider["tool_choice"] = tool_choice if tool_choice in _SUPPORTED_TOOL_CHOICES else "auto"

    reasoning = _safe_str(provider.get("reasoning_effort"), _safe_str(default.get("reasoning_effort"), "")).lower()
    provider["reasoning_effort"] = reasoning if reasoning in _SUPPORTED_REASONING_EFFORTS else ""

    thinking_default = _safe_bool(default.get("thinking_enabled"), False)
    if "thinking_enabled" in provider:
        thinking_enabled = _safe_bool(provider.get("thinking_enabled"), thinking_default)
    else:
        extra = provider.get("extra_body")
        thinking_enabled = bool(isinstance(extra, dict) and isinstance(extra.get("thinking"), dict)) or thinking_default
    provider["thinking_enabled"] = bool(thinking_enabled)
    provider["thinking_type"] = "enabled" if provider["thinking_enabled"] else ""

    extra_body = provider.get("extra_body") if isinstance(provider.get("extra_body"), dict) else {}
    extra_body = deepcopy(extra_body)
    if provider["thinking_enabled"]:
        extra_body["thinking"] = {"type": "enabled"}
    else:
        extra_body.pop("thinking", None)
    provider["extra_body"] = extra_body


def _merge_provider(default: dict[str, Any], saved: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(default)
    if isinstance(saved, dict):
        for key, value in saved.items():
            if key == "models" and isinstance(value, list):
                merged["models"] = _normalize_models(value)
            elif key == "api_key":
                if str(value or "").strip():
                    merged[key] = str(value).strip()
            else:
                merged[key] = value
    merged["key"] = _normalize_key(merged.get("key"), fallback=default.get("key", "provider"))
    merged["adapter"] = _normalize_adapter(merged.get("adapter"))
    merged["models"] = _normalize_models(merged.get("models", []))
    if not _safe_str(merged.get("current_model")) and merged["models"]:
        merged["current_model"] = str(merged["models"][0]["id"])
    _normalize_request_params(merged, default)
    return merged


def _normalize_adapter(value: object) -> str:
    adapter = _safe_str(value, "openai_compatible")
    return adapter if adapter in _SUPPORTED_ADAPTERS else "openai_compatible"


def _normalize_models(rows: object) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(rows, list):
        return out
    for item in rows:
        if isinstance(item, dict):
            model_id = _safe_str(item.get("id") or item.get("model"))
            label = _safe_str(item.get("label") or item.get("name"), model_id)
        else:
            model_id = _safe_str(item)
            label = model_id
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append({"id": model_id, "label": label or model_id})
    return out


def _normalize_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    saved = raw if isinstance(raw, dict) else {}
    providers: dict[str, dict[str, Any]] = {}
    saved_providers = saved.get("providers") if isinstance(saved.get("providers"), dict) else {}
    for key, default_provider in _DEFAULT_CONFIG["providers"].items():
        providers[key] = _merge_provider(default_provider, saved_providers.get(key))
    for key, saved_provider in saved_providers.items():
        provider_key = _normalize_key(saved_provider.get("key") if isinstance(saved_provider, dict) else key)
        if not provider_key or provider_key in providers or not isinstance(saved_provider, dict):
            continue
        custom_default = {
            "key": provider_key,
            "label": provider_key.upper(),
            "name": provider_key,
            "adapter": "openai_compatible",
            "base_url": "",
            "chat_path": "/chat/completions",
            "api_key_env": "",
            "default_model": "",
            "current_model": "",
            "enabled": True,
            "builtin": False,
            "balance_supported": False,
            "request_log_supported": True,
            "tool_call_supported": True,
            "tokenizer": "provider",
            "temperature": 0.7,
            "top_p": None,
            "presence_penalty": None,
            "frequency_penalty": None,
            "max_tokens": None,
            "seed": None,
            "timeout": 25,
            "tool_choice": "auto",
            "max_tool_steps": 6,
            "thinking_enabled": False,
            "thinking_type": "",
            "reasoning_effort": "",
            "extra_body": {},
            "models": [],
        }
        providers[provider_key] = _merge_provider(custom_default, saved_provider)
    current_provider = _normalize_key(saved.get("current_provider"), fallback="deepseek")
    if current_provider not in providers:
        current_provider = "deepseek"
    return {
        "current_provider": current_provider,
        "providers": providers,
    }


def load_llm_provider_config() -> dict[str, Any]:
    with _LOCK:
        try:
            raw = json.loads(_CONFIG_FILE.read_text(encoding="utf-8")) if _CONFIG_FILE.exists() else {}
        except Exception:
            raw = {}
        cfg = _normalize_config(raw)
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            _CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return cfg


def save_llm_provider_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = _normalize_config(config)
    with _LOCK:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def public_provider(provider: dict[str, Any], *, current_provider: str = "") -> dict[str, Any]:
    api_key = resolve_api_key(provider)
    return {
        "key": str(provider.get("key", "")),
        "provider": str(provider.get("key", "")),
        "label": str(provider.get("label", "") or provider.get("key", "")),
        "name": str(provider.get("name", "") or provider.get("label", "") or provider.get("key", "")),
        "adapter": str(provider.get("adapter", "openai_compatible")),
        "base_url": str(provider.get("base_url", "")),
        "chat_path": str(provider.get("chat_path", "")),
        "balance_path": str(provider.get("balance_path", "")),
        "api_key_env": str(provider.get("api_key_env", "")),
        "api_key_set": bool(api_key),
        "builtin": bool(provider.get("builtin", False)),
        "enabled": bool(provider.get("enabled", True)),
        "model": str(provider.get("current_model", "") or provider.get("default_model", "")),
        "current_model": str(provider.get("current_model", "") or provider.get("default_model", "")),
        "models": _normalize_models(provider.get("models", [])),
        "balance_supported": bool(provider.get("balance_supported", False)),
        "request_log_supported": bool(provider.get("request_log_supported", True)),
        "tool_call_supported": bool(provider.get("tool_call_supported", True)),
        "tokenizer": str(provider.get("tokenizer", "provider")),
        "temperature": provider.get("temperature"),
        "top_p": provider.get("top_p"),
        "presence_penalty": provider.get("presence_penalty"),
        "frequency_penalty": provider.get("frequency_penalty"),
        "max_tokens": provider.get("max_tokens"),
        "seed": provider.get("seed"),
        "timeout": provider.get("timeout"),
        "tool_choice": str(provider.get("tool_choice", "auto") or "auto"),
        "max_tool_steps": provider.get("max_tool_steps"),
        "thinking_enabled": bool(provider.get("thinking_enabled", False)),
        "thinking_type": str(provider.get("thinking_type", "") or ""),
        "reasoning_effort": str(provider.get("reasoning_effort", "") or ""),
        "extra_body": deepcopy(provider.get("extra_body")) if isinstance(provider.get("extra_body"), dict) else {},
        "is_current": str(provider.get("key", "")) == str(current_provider or ""),
    }


def provider_request_params(provider: dict[str, Any]) -> dict[str, Any]:
    """Return normalized request defaults for a provider."""
    return {
        "temperature": provider.get("temperature"),
        "top_p": provider.get("top_p"),
        "presence_penalty": provider.get("presence_penalty"),
        "frequency_penalty": provider.get("frequency_penalty"),
        "max_tokens": provider.get("max_tokens"),
        "seed": provider.get("seed"),
        "timeout": provider.get("timeout"),
        "tool_choice": str(provider.get("tool_choice", "auto") or "auto"),
        "max_tool_steps": provider.get("max_tool_steps"),
        "thinking_enabled": bool(provider.get("thinking_enabled", False)),
        "thinking_type": str(provider.get("thinking_type", "") or ""),
        "reasoning_effort": str(provider.get("reasoning_effort", "") or ""),
        "extra_body": deepcopy(provider.get("extra_body")) if isinstance(provider.get("extra_body"), dict) else {},
    }


def resolve_api_key(provider: dict[str, Any]) -> str:
    direct = _safe_str(provider.get("api_key"))
    if direct:
        return direct
    for key in (provider.get("api_key_env"), provider.get("api_key_env_fallback")):
        env_name = _safe_str(key)
        if env_name:
            value = _safe_str(os.getenv(env_name))
            if value:
                return value
    return ""


def upsert_provider(config: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    providers = config.setdefault("providers", {})
    key = _normalize_key(data.get("key"), fallback=_normalize_key(data.get("label"), fallback="custom"))
    existing = providers.get(key, {})
    builtin = bool(existing.get("builtin", False))
    provider = dict(existing) if isinstance(existing, dict) else {}
    provider.update({
        "key": key,
        "label": _safe_str(data.get("label"), provider.get("label") or key.upper()),
        "name": _safe_str(data.get("name"), provider.get("name") or key),
        "adapter": _normalize_adapter(data.get("adapter") or provider.get("adapter")),
        "base_url": _safe_str(data.get("base_url"), provider.get("base_url", "")),
        "chat_path": _safe_str(data.get("chat_path"), provider.get("chat_path", "/chat/completions")),
        "api_key_env": _safe_str(data.get("api_key_env"), provider.get("api_key_env", "")),
        "enabled": bool(data.get("enabled", provider.get("enabled", True))),
        "builtin": builtin,
        "balance_supported": bool(data.get("balance_supported", provider.get("balance_supported", False))),
        "request_log_supported": True,
        "tokenizer": _safe_str(data.get("tokenizer"), provider.get("tokenizer", "provider")),
    })
    for param_key in (
        "temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens",
        "seed", "timeout", "tool_choice", "max_tool_steps", "thinking_enabled",
        "reasoning_effort",
    ):
        if param_key in data:
            provider[param_key] = data.get(param_key)
    if "anthropic_version" in data:
        provider["anthropic_version"] = _safe_str(data.get("anthropic_version"), "2023-06-01")
    elif provider.get("adapter") == "anthropic_messages":
        provider.setdefault("anthropic_version", "2023-06-01")
    if "api_key" in data and _safe_str(data.get("api_key")):
        provider["api_key"] = _safe_str(data.get("api_key"))
    provider.setdefault("models", [])
    provider["models"] = _normalize_models(provider.get("models", []))
    if not _safe_str(provider.get("current_model")) and provider["models"]:
        provider["current_model"] = provider["models"][0]["id"]
    providers[key] = provider
    return save_llm_provider_config(config)


def upsert_model(config: dict[str, Any], provider_key: str, model_id: str, label: str = "") -> dict[str, Any]:
    key = _normalize_key(provider_key, fallback="deepseek")
    providers = config.setdefault("providers", {})
    if key not in providers:
        raise ValueError(f"provider not found: {key}")
    provider = providers[key]
    model_id = _safe_str(model_id)
    if not model_id:
        raise ValueError("model_id is required")
    models = _normalize_models(provider.get("models", []))
    label = _safe_str(label, model_id)
    replaced = False
    for item in models:
        if item["id"] == model_id:
            item["label"] = label
            replaced = True
            break
    if not replaced:
        models.append({"id": model_id, "label": label})
    provider["models"] = models
    provider["current_model"] = _safe_str(provider.get("current_model"), model_id)
    provider["default_model"] = _safe_str(provider.get("default_model"), model_id)
    return save_llm_provider_config(config)


def remove_model(config: dict[str, Any], provider_key: str, model_id: str) -> dict[str, Any]:
    key = _normalize_key(provider_key, fallback="deepseek")
    providers = config.setdefault("providers", {})
    if key not in providers:
        raise ValueError(f"provider not found: {key}")
    provider = providers[key]
    target = _safe_str(model_id)
    provider["models"] = [item for item in _normalize_models(provider.get("models", [])) if item["id"] != target]
    if provider.get("current_model") == target:
        provider["current_model"] = provider["models"][0]["id"] if provider["models"] else ""
    if provider.get("default_model") == target:
        provider["default_model"] = provider["current_model"]
    return save_llm_provider_config(config)
