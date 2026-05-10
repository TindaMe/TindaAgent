"""
Token estimation — DeepSeek V3 official tokenizer with heuristic fallback.

Attempts to load the exact tokenizer from ~/.tinda/agent/tokenizer/.
If transformers is not installed or the model is missing, falls back to a
lightweight heuristic (~4 chars/token ASCII, ~1.5 chars/token CJK).
"""

import logging
from pathlib import Path

LOGGER = logging.getLogger("tinda.tokenizer")

_TOKENIZER_DIR = Path.home() / ".tinda" / "agent" / "tokenizer"
_TOKENIZER_MODEL_PATH = _TOKENIZER_DIR / "tokenizer.json"
_TOKENIZER_CONFIG_PATH = _TOKENIZER_DIR / "tokenizer_config.json"

_tokenizer = None  # lazy-loaded HuggingFace tokenizer or None
_init_attempted = False


def _init_tokenizer() -> None:
    global _tokenizer, _init_attempted
    if _init_attempted:
        return
    _init_attempted = True
    if not _TOKENIZER_MODEL_PATH.exists():
        LOGGER.info("DeepSeek tokenizer model not found at %s, using heuristic", _TOKENIZER_MODEL_PATH)
        return
    try:
        import transformers
        _tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(_TOKENIZER_DIR), trust_remote_code=True
        )
        LOGGER.info("DeepSeek tokenizer loaded from %s", _TOKENIZER_DIR)
    except ImportError:
        LOGGER.info("transformers not installed, using heuristic token estimator")
    except Exception as exc:
        LOGGER.warning("Failed to load DeepSeek tokenizer: %s, using heuristic", exc)


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    _init_tokenizer()
    if _tokenizer is not None:
        try:
            return len(_tokenizer.encode(str(text)))
        except Exception:
            pass
    # Heuristic fallback
    s = str(text)
    non_ascii = sum(1 for c in s if ord(c) > 127)
    ascii_chars = len(s) - non_ascii
    return max(1, int(ascii_chars / 4.0 + non_ascii / 1.5))


def estimate_message_tokens(msg: dict) -> int:
    total = 4  # role + overhead
    content = msg.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
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


def format_token_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)
