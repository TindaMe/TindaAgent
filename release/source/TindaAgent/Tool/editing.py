from __future__ import annotations

import difflib
import hashlib
import fnmatch
from pathlib import Path
from typing import Any


MAX_EDIT_BYTES = 2_000_000
MAX_SEARCH_FILE_BYTES = 1_000_000
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
}


def _resolve_edit_path(path: str) -> Path:
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("path is required")
    return Path(raw).expanduser().resolve()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text_file(path: str, *, max_bytes: int = MAX_EDIT_BYTES) -> dict[str, Any]:
    target = _resolve_edit_path(path)
    if not target.exists():
        return {"ok": False, "error": "file not found", "path": str(target)}
    if not target.is_file():
        return {"ok": False, "error": "path is not a file", "path": str(target)}
    size = target.stat().st_size
    if size > int(max_bytes):
        return {"ok": False, "error": f"file too large: {size} bytes", "path": str(target), "size": size}
    text = target.read_text(encoding="utf-8")
    return {"ok": True, "path": str(target), "content": text, "sha256": _sha256(text), "size": size}


def search_text_files(
    root: str = ".",
    query: str = "",
    content: str = "",
    glob: str = "*",
    max_results: int = 50,
    max_depth: int = 8,
) -> dict[str, Any]:
    base = _resolve_edit_path(root or ".")
    if not base.exists():
        return {"ok": False, "error": "root not found", "root": str(base)}
    if not base.is_dir():
        return {"ok": False, "error": "root is not a directory", "root": str(base)}

    name_query = str(query or "").strip().lower()
    content_query = str(content or "")
    pattern = str(glob or "*").strip() or "*"
    limit = max(1, min(int(max_results or 50), 200))
    depth_limit = max(0, min(int(max_depth or 8), 32))
    results: list[dict[str, Any]] = []
    scanned = 0
    skipped = 0

    def depth(path: Path) -> int:
        try:
            rel = path.relative_to(base)
        except ValueError:
            return 0
        return len(rel.parts) - 1

    for path in sorted(base.rglob("*")):
        if len(results) >= limit:
            break
        try:
            if any(part in DEFAULT_EXCLUDE_DIRS for part in path.relative_to(base).parts):
                skipped += 1
                continue
        except ValueError:
            continue
        if depth(path) > depth_limit:
            skipped += 1
            continue
        if not path.is_file():
            continue
        if not fnmatch.fnmatch(path.name, pattern):
            continue
        if name_query and name_query not in path.name.lower() and name_query not in str(path.relative_to(base)).lower():
            continue
        scanned += 1
        row: dict[str, Any] = {
            "path": str(path),
            "relative_path": str(path.relative_to(base)),
            "size": path.stat().st_size,
        }
        if content_query:
            if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                skipped += 1
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped += 1
                continue
            matched = False
            for line_no, line in enumerate(text.splitlines(), start=1):
                idx = line.find(content_query)
                if idx < 0:
                    continue
                start = max(0, idx - 80)
                end = min(len(line), idx + len(content_query) + 80)
                row["line"] = line_no
                row["snippet"] = line[start:end]
                matched = True
                break
            if not matched:
                continue
        results.append(row)

    return {
        "ok": True,
        "root": str(base),
        "query": name_query,
        "content_query": bool(content_query),
        "glob": pattern,
        "scanned": scanned,
        "skipped": skipped,
        "truncated": len(results) >= limit,
        "results": results,
    }


def apply_text_edit(
    path: str,
    *,
    old_text: str,
    new_text: str,
    expected_sha256: str = "",
    create: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    target = _resolve_edit_path(path)
    old = str(old_text or "")
    new = str(new_text or "")
    if old == new and not create:
        return {"ok": False, "error": "old_text and new_text are identical", "path": str(target)}

    if target.exists():
        if not target.is_file():
            return {"ok": False, "error": "path is not a file", "path": str(target)}
        if target.stat().st_size > MAX_EDIT_BYTES:
            return {"ok": False, "error": f"file too large: {target.stat().st_size} bytes", "path": str(target)}
        before = target.read_text(encoding="utf-8")
    else:
        if not bool(create):
            return {"ok": False, "error": "file not found; set create=true to create it", "path": str(target)}
        before = ""

    before_hash = _sha256(before)
    expected = str(expected_sha256 or "").strip()
    if expected and expected != before_hash:
        return {
            "ok": False,
            "error": "sha256 mismatch",
            "path": str(target),
            "expected_sha256": expected,
            "actual_sha256": before_hash,
        }

    if create and not target.exists() and old:
        return {"ok": False, "error": "old_text must be empty when creating a new file", "path": str(target)}

    if old:
        count = before.count(old)
        if count <= 0:
            return {"ok": False, "error": "old_text not found", "path": str(target), "sha256": before_hash}
        if count > 1:
            return {"ok": False, "error": "old_text is not unique", "path": str(target), "matches": count, "sha256": before_hash}
        after = before.replace(old, new, 1)
    else:
        if not create:
            return {"ok": False, "error": "old_text is required unless create=true", "path": str(target)}
        after = new

    diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=str(target),
            tofile=str(target),
            lineterm="",
            n=3,
        )
    )
    result = {
        "ok": True,
        "path": str(target),
        "dry_run": bool(dry_run),
        "before_sha256": before_hash,
        "after_sha256": _sha256(after),
        "changed": before != after,
        "diff": diff[:16000],
    }
    if dry_run or before == after:
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(after, encoding="utf-8")
    return result
