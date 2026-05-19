"""Token accounting with DeepSeek API usage first, official tokenizer second."""

import json
import logging
import os
from pathlib import Path
import tempfile
import urllib.request
import zipfile
from typing import Any

LOGGER = logging.getLogger("tinda.tokenizer")

_TOKENIZER_DIR = Path.home() / ".tinda" / "agent" / "tokenizer"
_TOKENIZER_MODEL_PATH = _TOKENIZER_DIR / "tokenizer.json"
_TOKENIZER_CONFIG_PATH = _TOKENIZER_DIR / "tokenizer_config.json"
_TOKENIZER_ZIP_URL = "https://cdn.deepseek.com/api-docs/deepseek_v3_tokenizer.zip"

_tokenizer = None  # lazy-loaded tokenizers.Tokenizer or None
_tokenizer_config: dict[str, Any] | None = None
_init_attempted = False
_last_error = ""


def _auto_download_enabled() -> bool:
    value = str(os.getenv("TINDA_DEEPSEEK_TOKENIZER_AUTO_DOWNLOAD", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def ensure_official_tokenizer_files(*, download: bool = True) -> bool:
    """Ensure DeepSeek's official tokenizer files exist in ~/.tinda/agent/tokenizer."""
    global _last_error
    if _TOKENIZER_MODEL_PATH.exists() and _TOKENIZER_CONFIG_PATH.exists():
        return True
    if not download:
        return False
    try:
        _TOKENIZER_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / "deepseek_v3_tokenizer.zip"
            urllib.request.urlretrieve(_TOKENIZER_ZIP_URL, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                for name in ("tokenizer.json", "tokenizer_config.json"):
                    member = f"deepseek_v3_tokenizer/{name}"
                    target = _TOKENIZER_DIR / name
                    target.write_bytes(zf.read(member))
        LOGGER.info("DeepSeek tokenizer files installed to %s", _TOKENIZER_DIR)
        return _TOKENIZER_MODEL_PATH.exists() and _TOKENIZER_CONFIG_PATH.exists()
    except Exception as exc:
        _last_error = str(exc)
        LOGGER.warning("Failed to install DeepSeek tokenizer files: %s", exc)
        return False


def _init_tokenizer() -> None:
    global _tokenizer, _tokenizer_config, _init_attempted, _last_error
    if _init_attempted:
        return
    _init_attempted = True
    if not ensure_official_tokenizer_files(download=_auto_download_enabled()):
        LOGGER.info("DeepSeek tokenizer model not found at %s", _TOKENIZER_MODEL_PATH)
        return
    try:
        from tokenizers import Tokenizer
        _tokenizer = Tokenizer.from_file(str(_TOKENIZER_MODEL_PATH))
        _tokenizer_config = json.loads(_TOKENIZER_CONFIG_PATH.read_text(encoding="utf-8"))
        LOGGER.info("DeepSeek official tokenizer loaded from %s", _TOKENIZER_MODEL_PATH)
    except ImportError:
        _last_error = "tokenizers not installed"
        LOGGER.info("tokenizers not installed; official offline token count unavailable")
    except Exception as exc:
        _last_error = str(exc)
        LOGGER.warning("Failed to load DeepSeek tokenizer: %s", exc)


def tokenizer_status() -> dict[str, Any]:
    _init_tokenizer()
    return {
        "engine": "deepseek_official" if _tokenizer is not None else "unavailable",
        "official_files": bool(_TOKENIZER_MODEL_PATH.exists() and _TOKENIZER_CONFIG_PATH.exists()),
        "tokenizers": bool(_tokenizer is not None),
        "tokenizer_dir": str(_TOKENIZER_DIR),
        "last_error": _last_error,
    }


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    _init_tokenizer()
    if _tokenizer is not None:
        try:
            return len(_tokenizer.encode(str(text)).ids)
        except Exception:
            pass
    return 0


def estimate_message_tokens(msg: dict) -> int:
    total = 4  # role + overhead
    content = msg.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        total += estimate_tokens(reasoning)
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                if isinstance(fn, dict):
                    total += estimate_tokens(fn.get("name", ""))
                    total += estimate_tokens(fn.get("arguments", ""))
    if msg.get("role") == "tool" and isinstance(content, str):
        total += 2
    return total


def estimate_messages_tokens(messages: list[dict]) -> int:
    if not messages:
        return 0
    return sum(estimate_message_tokens(m) for m in messages)


def _special_token_content(name: str) -> str:
    cfg = _tokenizer_config if isinstance(_tokenizer_config, dict) else {}
    value = cfg.get(name)
    if isinstance(value, dict):
        return str(value.get("content", "") or "")
    return str(value or "")


def _render_chat_template(messages: list[dict]) -> str:
    if not isinstance(_tokenizer_config, dict):
        return ""
    template = str(_tokenizer_config.get("chat_template", "") or "")
    if not template:
        return ""
    try:
        from jinja2 import Environment
        env = Environment(autoescape=False)
        return env.from_string(template).render(
            messages=messages,
            add_generation_prompt=True,
            bos_token=_special_token_content("bos_token"),
            eos_token=_special_token_content("eos_token"),
        )
    except Exception as exc:
        LOGGER.warning("DeepSeek chat template render failed: %s", exc)
        return ""


def estimate_request_messages_tokens(messages: list[dict]) -> int:
    safe_messages = [m for m in (messages or []) if isinstance(m, dict)]
    if not safe_messages:
        return 0
    _init_tokenizer()
    if _tokenizer is not None:
        try:
            rendered = _render_chat_template(safe_messages)
            if not rendered:
                return estimate_messages_tokens(safe_messages)
            reasoning_tokens = 0
            for msg in safe_messages:
                rc = msg.get("reasoning_content")
                if isinstance(rc, str) and rc.strip():
                    reasoning_tokens += estimate_tokens(rc)
            return int(len(_tokenizer.encode(rendered).ids) + reasoning_tokens)
        except Exception as exc:
            LOGGER.warning("DeepSeek official tokenizer count failed: %s", exc)
    return estimate_messages_tokens(safe_messages)


def estimate_request_token_usage(payload: dict[str, Any] | None) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    messages = body.get("messages")
    tools = body.get("tools")
    message_tokens = estimate_request_messages_tokens(messages if isinstance(messages, list) else [])
    tool_tokens = 0
    if isinstance(tools, list) and tools:
        tool_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False, sort_keys=True))
    payload_tokens = estimate_tokens(json.dumps(body, ensure_ascii=False, sort_keys=True)) if body else 0
    total = int(message_tokens + tool_tokens)
    return {
        "total": total,
        "messages": int(message_tokens),
        "tools": int(tool_tokens),
        "payload": int(payload_tokens),
        "source": "official_tokenizer" if tokenizer_status().get("engine") == "deepseek_official" else "unavailable",
        "tokenizer": tokenizer_status(),
    }


def format_token_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)
