from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from TindaAgent.Process.Architecture.paths import get_runtime_root

MAX_SKILL_BYTES = 200_000


def _skill_roots() -> list[Path]:
    roots = [get_runtime_root() / "skills"]
    raw = str(os.getenv("TINDA_SKILL_PATHS", "") or "").strip()
    for item in raw.split(os.pathsep):
        if item.strip():
            roots.append(Path(item).expanduser())
    return roots


def _extract_description(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue
        if clean.lower().startswith("description:"):
            return clean.split(":", 1)[1].strip()
        return clean[:240]
    return ""


def _safe_skill_name(name: str) -> str:
    clean = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", clean):
        raise ValueError("invalid skill name")
    return clean


def discover_skills() -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _skill_roots():
        if not root.exists() or not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            name = skill_md.parent.name
            if name in seen:
                continue
            seen.add(name)
            try:
                text = skill_md.read_text(encoding="utf-8")[:4000]
            except Exception:
                text = ""
            skills.append({
                "name": name,
                "path": str(skill_md),
                "description": _extract_description(text),
            })
    return {"ok": True, "roots": [str(x) for x in _skill_roots()], "skills": skills}


def read_skill(name: str) -> dict[str, Any]:
    skill_name = _safe_skill_name(name)
    for root in _skill_roots():
        skill_md = root / skill_name / "SKILL.md"
        if not skill_md.exists() or not skill_md.is_file():
            continue
        size = skill_md.stat().st_size
        if size > MAX_SKILL_BYTES:
            return {"ok": False, "error": f"skill too large: {size} bytes", "name": skill_name, "path": str(skill_md)}
        text = skill_md.read_text(encoding="utf-8")
        return {
            "ok": True,
            "name": skill_name,
            "path": str(skill_md),
            "content": text,
            "description": _extract_description(text),
        }
    return {"ok": False, "error": "skill not found", "name": skill_name}
