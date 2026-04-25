from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from TindaAgent.Process.Architecture.paths import get_sessions_root


def _now_tag() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".jsonl.tmp")
    payload = "\n".join(json.dumps(x, ensure_ascii=False) for x in rows)
    temp.write_text(payload + ("\n" if payload else ""), encoding="utf-8")
    temp.replace(path)


def _is_tool_marker(row: dict) -> bool:
    return str(row.get("entry_type", "")).strip() == "tool_marker"


def _is_terminal(row: dict) -> bool:
    return str(row.get("entry_type", "")).strip() == "terminal"


def _is_tool_cmd_terminal(row: dict) -> bool:
    if not _is_terminal(row):
        return False
    if str(row.get("terminal_kind", "")).strip() != "cmd":
        return False
    content = str(row.get("content", "")).lstrip()
    return content.startswith("[tool] ")


@dataclass
class RepairStat:
    changed: bool
    moved_blocks: int
    moved_entries: int


def _repair_rows(rows: list[dict]) -> tuple[list[dict], RepairStat]:
    fixed = [dict(x) for x in rows]
    moved_blocks = 0
    moved_entries = 0

    i = 0
    while i < len(fixed):
        if not _is_tool_marker(fixed[i]):
            i += 1
            continue

        marker_idx = i
        next_marker_idx = len(fixed)
        j = marker_idx + 1
        while j < len(fixed):
            if _is_tool_marker(fixed[j]):
                next_marker_idx = j
                break
            j += 1

        cmd_idx = marker_idx + 1
        while cmd_idx < next_marker_idx and not _is_tool_cmd_terminal(fixed[cmd_idx]):
            cmd_idx += 1

        if cmd_idx >= next_marker_idx:
            i = next_marker_idx
            continue

        block_start = cmd_idx
        k = cmd_idx - 1
        while k > marker_idx and _is_terminal(fixed[k]):
            block_start = k
            k -= 1

        block_end = cmd_idx
        while block_end + 1 < next_marker_idx and _is_terminal(fixed[block_end + 1]):
            block_end += 1

        target_idx = marker_idx + 1
        if block_start != target_idx:
            block = fixed[block_start : block_end + 1]
            del fixed[block_start : block_end + 1]
            fixed[target_idx:target_idx] = block
            moved_blocks += 1
            moved_entries += len(block)

        i = next_marker_idx

    return fixed, RepairStat(
        changed=(moved_blocks > 0),
        moved_blocks=moved_blocks,
        moved_entries=moved_entries,
    )


def _iter_target_files(messages_dir: Path, session_ids: list[str]) -> list[Path]:
    if session_ids:
        out: list[Path] = []
        for sid in session_ids:
            path = messages_dir / f"{sid}.jsonl"
            if path.exists():
                out.append(path)
        return out
    return sorted(messages_dir.glob("*.jsonl"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="修复历史会话中工具终端输出错位（将 terminal block 归位到对应 tool_marker 后）"
    )
    parser.add_argument(
        "--sessions-root",
        default=str(get_sessions_root()),
        help="Sessions 根目录（默认: ~/.tinda/agent/Data/Sessions）",
    )
    parser.add_argument(
        "--session-id",
        action="append",
        default=[],
        help="仅修复指定会话（可重复）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行落盘；默认仅 dry-run 预览",
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="备份目录（仅 --apply 生效）",
    )
    args = parser.parse_args()

    sessions_root = Path(args.sessions_root).resolve()
    messages_dir = sessions_root / "messages"
    if not messages_dir.exists():
        print(f"[error] messages 目录不存在: {messages_dir}")
        return 2

    targets = _iter_target_files(messages_dir, [str(x).strip() for x in args.session_id if str(x).strip()])
    if not targets:
        print("[info] 未找到可处理的会话文件。")
        return 0

    backup_dir = None
    if args.apply:
        if args.backup_dir:
            backup_dir = Path(args.backup_dir).resolve()
        else:
            backup_dir = sessions_root / f"messages_backup_terminal_fix_{_now_tag()}"
        backup_dir.mkdir(parents=True, exist_ok=True)

    changed_files = 0
    total_blocks = 0
    total_entries = 0

    for path in targets:
        rows = _read_jsonl(path)
        fixed, stat = _repair_rows(rows)
        if not stat.changed:
            print(f"[skip] {path.name}: no change")
            continue

        changed_files += 1
        total_blocks += stat.moved_blocks
        total_entries += stat.moved_entries
        print(
            f"[plan] {path.name}: move blocks={stat.moved_blocks}, entries={stat.moved_entries}"
        )

        if args.apply and backup_dir is not None:
            backup_path = backup_dir / path.name
            backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            _write_jsonl(path, fixed)
            print(f"[done] {path.name} -> backup: {backup_path}")

    mode = "apply" if args.apply else "dry-run"
    print(
        f"[summary] mode={mode}, files={len(targets)}, changed_files={changed_files}, "
        f"moved_blocks={total_blocks}, moved_entries={total_entries}"
    )
    if args.apply and backup_dir is not None:
        print(f"[backup] {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
