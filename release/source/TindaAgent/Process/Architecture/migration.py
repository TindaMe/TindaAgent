from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from TindaAgent.Process.Architecture import paths
from TindaAgent.Process.Observability import audit_event

_THIS_FILE = str(Path(__file__).resolve())


def _now_tag() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def _copytree_merge(src: Path, dst: Path) -> tuple[int, int]:
    copied = 0
    failed = 0
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        target = dst / rel
        try:
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    src_stat = path.stat()
                    dst_stat = target.stat()
                    src_mtime = int(getattr(src_stat, "st_mtime", 0))
                    dst_mtime = int(getattr(dst_stat, "st_mtime", 0))
                    src_size = int(getattr(src_stat, "st_size", -1))
                    dst_size = int(getattr(dst_stat, "st_size", -2))
                    if src_size == dst_size and src_mtime <= dst_mtime:
                        continue
                shutil.copy2(path, target)
                copied += 1
        except Exception:
            failed += 1
    return copied, failed


def _copy_file_if_missing(src: Path, dst: Path) -> tuple[int, int]:
    if not src.exists() or not src.is_file():
        return 0, 0
    if dst.exists():
        return 0, 0
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1, 0
    except Exception:
        return 0, 1


def _has_any_file(root: Path) -> bool:
    if not root.exists():
        return False
    for p in root.rglob("*"):
        if p.is_file():
            return True
    return False


def _backup_legacy_dir(src: Path) -> Path | None:
    if not src.exists():
        return None
    backup = src.with_name(f"{src.name}_backup_{_now_tag()}")
    try:
        src.rename(backup)
        return backup
    except Exception:
        return None


def bootstrap_storage() -> dict:
    paths.ensure_runtime_dirs()

    runtime_root = paths.get_runtime_root()
    runtime_data = paths.get_data_root()
    runtime_log = paths.get_log_root()
    runtime_sessions = paths.get_sessions_root()
    runtime_users = paths.get_users_file()
    runtime_memory = paths.get_memory_file()
    runtime_shared = paths.get_shared_root()
    legacy_data = paths.get_legacy_data_root()
    legacy_log = paths.get_legacy_log_root()
    legacy_sessions = paths.get_legacy_sessions_root()
    legacy_users = paths.get_legacy_users_file()
    legacy_memory = paths.get_legacy_memory_file()

    result = {
        "runtime_root": str(runtime_root),
        "copied_files": 0,
        "failed_items": 0,
        "used_legacy_read_fallback": False,
        "legacy_data_backup": "",
        "legacy_log_backup": "",
        "shared_data_bootstrapped": False,
    }

    # 若运行目录就是项目目录本身，不执行“重命名旧目录”动作，避免误改开发目录结构。
    allow_rename_legacy = runtime_root.resolve() != paths.get_project_root().resolve()

    audit_event(
        op_type="SYSTEM_EXECUTE",
        subsystem="storage_migration",
        func="bootstrap_storage",
        file_path=_THIS_FILE,
        content="migration_start",
        extra={"runtime_root": str(runtime_root)},
    )

    copied, failed = 0, 0

    try:
        data_copied = 0
        data_failed = 0
        if legacy_data.exists() and legacy_data.resolve() != runtime_data.resolve():
            c, f = _copytree_merge(legacy_data, runtime_data)
            copied += c
            failed += f
            data_copied += c
            data_failed += f
            if data_copied > 0 and data_failed == 0 and allow_rename_legacy:
                backup = _backup_legacy_dir(legacy_data)
                if backup is not None:
                    result["legacy_data_backup"] = str(backup)

        log_copied = 0
        log_failed = 0
        if legacy_log.exists() and legacy_log.resolve() != runtime_log.resolve():
            c, f = _copytree_merge(legacy_log, runtime_log)
            copied += c
            failed += f
            log_copied += c
            log_failed += f
            if log_copied > 0 and log_failed == 0 and allow_rename_legacy:
                backup = _backup_legacy_dir(legacy_log)
                if backup is not None:
                    result["legacy_log_backup"] = str(backup)

        # legacy 兼容兜底：当旧目录未重命名（例如复制失败）时，补拷已知关键文件。
        c, f = _copy_file_if_missing(legacy_users, runtime_users)
        copied += c
        failed += f
        c, f = _copy_file_if_missing(legacy_memory, runtime_memory)
        copied += c
        failed += f
        if legacy_sessions.exists() and runtime_sessions.exists():
            c, f = _copytree_merge(legacy_sessions, runtime_sessions)
            copied += c
            failed += f

        if failed > 0:
            result["used_legacy_read_fallback"] = True
            audit_event(
                op_type="SYSTEM_EXECUTE",
                subsystem="storage_migration",
                func="bootstrap_storage",
                file_path=_THIS_FILE,
                content="migration_partial_failed",
                extra={"copied_files": copied, "failed_items": failed},
            )
        else:
            audit_event(
                op_type="SYSTEM_EXECUTE",
                subsystem="storage_migration",
                func="bootstrap_storage",
                file_path=_THIS_FILE,
                content="migration_done",
                extra={"copied_files": copied, "failed_items": failed},
            )

    except Exception as e:
        result["used_legacy_read_fallback"] = True
        failed += 1
        audit_event(
            op_type="SYSTEM_EXECUTE",
            subsystem="storage_migration",
            func="bootstrap_storage",
            file_path=_THIS_FILE,
            content=f"migration_exception err={e}",
            extra={"ok": False},
        )

    result["copied_files"] = copied
    result["failed_items"] = failed

    # 建立共享数据目录镜像入口（后续版本切换/迁移以 shared/data 为主）
    try:
        shared_data = runtime_shared / "data"
        if not shared_data.exists():
            shared_data.mkdir(parents=True, exist_ok=True)
            if runtime_data.exists():
                _copytree_merge(runtime_data, shared_data)
        result["shared_data_bootstrapped"] = True
    except Exception:
        result["shared_data_bootstrapped"] = False

    # 若迁移失败但新路径为空，允许读取旧路径作为兼容兜底
    if failed > 0 and (not _has_any_file(runtime_data) or not _has_any_file(runtime_log)):
        result["used_legacy_read_fallback"] = True

    return result
