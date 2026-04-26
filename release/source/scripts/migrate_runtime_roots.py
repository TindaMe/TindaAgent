#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Stat:
    copied: int = 0
    skipped: int = 0
    failed: int = 0


def _copytree_merge(src: Path, dst: Path, stat: Stat) -> None:
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        target = dst / rel
        try:
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not path.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                src_stat = path.stat()
                dst_stat = target.stat()
                src_mtime = int(getattr(src_stat, "st_mtime", 0))
                dst_mtime = int(getattr(dst_stat, "st_mtime", 0))
                src_size = int(getattr(src_stat, "st_size", -1))
                dst_size = int(getattr(dst_stat, "st_size", -2))
                if src_size == dst_size and src_mtime <= dst_mtime:
                    stat.skipped += 1
                    continue
            shutil.copy2(path, target)
            stat.copied += 1
        except Exception:
            stat.failed += 1


def _copy_file_if_missing(src: Path, dst: Path, stat: Stat) -> None:
    if not src.exists() or not src.is_file():
        return
    if dst.exists():
        stat.skipped += 1
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        stat.copied += 1
    except Exception:
        stat.failed += 1


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_runtime(src_root: Path, dst_root: Path) -> dict:
    src_root = src_root.resolve()
    dst_root = dst_root.resolve()
    stat = Stat()
    report: dict = {
        "ok": True,
        "time": _now_iso(),
        "source": str(src_root),
        "target": str(dst_root),
        "copied_files": 0,
        "skipped_files": 0,
        "failed_items": 0,
        "steps": [],
    }

    if not src_root.exists():
        return {**report, "ok": False, "error": "source runtime root not found"}
    dst_root.mkdir(parents=True, exist_ok=True)

    # 1) 合并核心目录
    for name in ("Data", "log", "versions", "shared", "trust", "migrations"):
        src = src_root / name
        dst = dst_root / name
        if src.exists():
            _copytree_merge(src, dst, stat)
            report["steps"].append(f"merged_dir:{name}")

    # 2) 合并 current.json（仅当目标缺失时复制）
    _copy_file_if_missing(src_root / "current.json", dst_root / "current.json", stat)
    report["steps"].append("merged_file:current.json_if_missing")

    # 3) 优先保留“版本更多的一方” current.json
    src_versions = list((src_root / "versions").glob("*")) if (src_root / "versions").exists() else []
    dst_versions = list((dst_root / "versions").glob("*")) if (dst_root / "versions").exists() else []
    if len(src_versions) > len(dst_versions):
        src_cur = src_root / "current.json"
        if src_cur.exists():
            try:
                shutil.copy2(src_cur, dst_root / "current.json")
                stat.copied += 1
                report["steps"].append("override_current_from_source")
            except Exception:
                stat.failed += 1

    # 4) 记录迁移报告
    report["copied_files"] = stat.copied
    report["skipped_files"] = stat.skipped
    report["failed_items"] = stat.failed
    report["source_current"] = _read_json(src_root / "current.json")
    report["target_current"] = _read_json(dst_root / "current.json")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge runtime data from one root to another.")
    parser.add_argument("--src", default="/mnt/e/.tinda/agent", help="source runtime root")
    parser.add_argument("--dst", default="/home/tinda/.tinda/agent", help="target runtime root")
    parser.add_argument("--report", default="/home/tinda/.tinda/agent/migrations/runtime_migration_report.json")
    args = parser.parse_args()

    report = _merge_runtime(Path(args.src), Path(args.dst))
    _write_json(Path(args.report), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

