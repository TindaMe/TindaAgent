from __future__ import annotations

import importlib.metadata
import re
from functools import lru_cache
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_version_from_pyproject() -> str | None:
    pyproject = _project_root() / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        return None
    value = str(m.group(1)).strip()
    return value or None


def _read_version_from_metadata() -> str | None:
    try:
        value = str(importlib.metadata.version("TindaAgent")).strip()
    except Exception:
        return None
    return value or None


@lru_cache(maxsize=1)
def get_app_version() -> str:
    # 以源码 pyproject 为准，避免本地残留 egg-info/安装包旧版本污染显示。
    by_project = _read_version_from_pyproject()
    if by_project:
        return by_project
    by_meta = _read_version_from_metadata()
    if by_meta:
        return by_meta
    return "0.0.0"

