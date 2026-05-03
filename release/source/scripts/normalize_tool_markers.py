#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


MARKER_BLOCK_MIN = 2


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _dump_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "\n".join(json.dumps(x, ensure_ascii=False) for x in rows)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _extract_call_ids_for_turn(rows: list[dict[str, Any]], idx: int) -> list[str]:
    """
    仅按“当前轮次”提取 call_id：
    - 起点：当前行 idx
    - 终点：下一个 user/chat 之前
    这样避免把历史轮次 call_id 混入当前 marker。
    """
    out: list[str] = []
    seen: set[str] = set()
    end = len(rows)
    for j in range(idx + 1, len(rows)):
        row = rows[j]
        if str(row.get("role", "") or "") == "user" and str(row.get("entry_type", "") or "") == "chat":
            end = j
            break

    pattern_hash = re.compile(r"#([A-Za-z0-9_.:-]+)")
    current_call_id = ""
    current_ok: bool | None = None
    in_tool_block = False

    def _flush_current() -> None:
        nonlocal current_call_id, current_ok, in_tool_block
        if in_tool_block and current_call_id and current_ok is not False and current_call_id not in seen:
            seen.add(current_call_id)
            out.append(current_call_id)
        current_call_id = ""
        current_ok = None
        in_tool_block = False

    for j in range(idx, end):
        row = rows[j]
        et = str(row.get("entry_type", "") or "")
        if et != "terminal":
            continue
        text = str(row.get("content", "") or "")
        if not text:
            continue
        if text.strip().startswith("[tool]"):
            _flush_current()
            in_tool_block = True
            for m in pattern_hash.finditer(text):
                current_call_id = str(m.group(1) or "").strip()
                if current_call_id:
                    break
            continue
        if not in_tool_block:
            continue
        stripped = text.strip()
        if stripped.startswith("\"ok\":"):
            current_ok = ("true" in stripped.lower())
            continue
        if stripped.startswith("─"):
            _flush_current()
            continue
    _flush_current()
    return out


def _build_marker(call_ids: list[str]) -> str:
    lines = ["> >_<", "> --工具调用中--"]
    for cid in call_ids:
        lines.append(f"> --call_id: {cid}--")
    return "\n".join(lines)


def _strip_existing_markers(text: str) -> str:
    lines = str(text or "").splitlines()
    kept: list[str] = []
    for line in lines:
        s = line.strip()
        if s in {"> >_<", "> --调用工具中--", "> --工具调用中--"}:
            continue
        if s.startswith("> --call_id:"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _looks_like_has_tool_marker(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    return (
        ("> >_<" in t)
        or ("--调用工具中--" in t)
        or ("--工具调用中--" in t)
        or ("[工具调用]" in t)
    )


def _normalize_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    changed = 0
    out = [dict(x) for x in rows]
    for i, row in enumerate(out):
        role = str(row.get("role", "") or "")
        et = str(row.get("entry_type", "") or "")
        if role != "assistant":
            continue
        if et not in {"chat", "tool_marker"}:
            continue
        raw = str(row.get("content", "") or "")
        if not _looks_like_has_tool_marker(raw):
            continue
        body = _strip_existing_markers(raw)
        call_ids = _extract_call_ids_for_turn(out, i)
        marker = _build_marker(call_ids)
        merged = f"{body}\n\n{marker}" if body else marker
        merged = merged.strip()
        if merged != raw.strip():
            row["content"] = merged
            changed += 1
    return out, changed


def _iter_target_files(messages_dir: Path, session_id: str) -> list[Path]:
    if session_id:
        p = messages_dir / f"{session_id}.jsonl"
        return [p] if p.exists() else []
    return sorted(messages_dir.glob("*.jsonl"))


def _resolve_sessions_dir(repo_root: Path, data_root: str) -> Path:
    if data_root:
        return Path(data_root).expanduser().resolve() / "Sessions" / "messages"

    # 优先兼容你当前环境（/mnt/e/.tinda/agent），其次回落到 ~/.tinda/agent。
    candidates = [
        Path("/mnt/e/.tinda/agent/Data/Sessions/messages"),
        Path.home() / ".tinda" / "agent" / "Data" / "Sessions" / "messages",
        repo_root / "Data" / "Sessions" / "messages",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize legacy duplicated tool markers in session jsonl.")
    parser.add_argument("--session", default="", help="target session id, e.g. s_xxx")
    parser.add_argument("--data-root", default="", help="runtime Data root, e.g. /mnt/e/.tinda/agent/Data")
    parser.add_argument("--dry-run", action="store_true", help="preview only, do not write")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    messages_dir = _resolve_sessions_dir(repo_root, str(args.data_root or "").strip())
    files = _iter_target_files(messages_dir, str(args.session or "").strip())
    if not files:
        print(f"[normalize] no target files under: {messages_dir}")
        return 0

    changed_files = 0
    changed_rows = 0
    for f in files:
        rows = _load_jsonl(f)
        if not rows:
            continue
        new_rows, c = _normalize_rows(rows)
        if c <= 0:
            continue
        changed_files += 1
        changed_rows += c
        if not args.dry_run:
            _dump_jsonl(f, new_rows)
        print(f"[normalize] {'would_update' if args.dry_run else 'updated'} {f.name}: rows={c}")

    print(f"[summary] files={len(files)} changed_files={changed_files} changed_rows={changed_rows} dry_run={bool(args.dry_run)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
