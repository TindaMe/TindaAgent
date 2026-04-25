from __future__ import annotations

import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_LOG_FILE = Path(__file__).resolve().parent.parent / "Data" / "Log" / "error.log"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_text(value: Any, max_len: int = 400) -> str:
    text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "...(truncated)"
    return text


def _write_block(block: str) -> None:
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(block)


def log_error(context: str, message: str, **meta: Any) -> None:
    pairs = [f"{k}={_safe_text(v)}" for k, v in meta.items()]
    meta_text = " ".join(pairs).strip()
    line = f"[{_now_iso()}] [{context}] {message}"
    if meta_text:
        line = f"{line} | {meta_text}"
    _write_block(line + "\n")


def log_exception(context: str, exc: BaseException, **meta: Any) -> None:
    pairs = [f"{k}={_safe_text(v)}" for k, v in meta.items()]
    meta_text = " ".join(pairs).strip()
    head = (
        f"[{_now_iso()}] [{context}] "
        f"{exc.__class__.__name__}: {_safe_text(exc, max_len=1200)}"
    )
    if meta_text:
        head = f"{head} | {meta_text}"
    tb = traceback.format_exc()
    block = f"{head}\n{tb}---\n"
    _write_block(block)
