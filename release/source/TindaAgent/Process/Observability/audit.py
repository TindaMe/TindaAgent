from __future__ import annotations

import gzip
import json
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from TindaAgent.Process.Architecture.paths import get_log_root

_OP_VALUE_MAP = {
    "PUBLIC_READ": "PUBLIC_READ",
    "PUBLIC_WRITE": "PUBLIC_WRITE",
    "PUBLIC_EXECUTE": "PUBLIC_EXECUTE",
    "TOOL_READ": "TOOL_READ",
    "TOOL_WRITE": "TOOL_WRITE",
    "TOOL_EXECUTE": "TOOL_EXECUTE",
    "SYSTEM_READ": "SYSTEM_READ",
    "SYSTEM_WRITE": "SYSTEM_WRITE",
    "SYSTEM_EXECUTE": "SYSTEM_EXECUTE",
}

OP_PUBLIC_READ = "PUBLIC_READ"
OP_PUBLIC_WRITE = "PUBLIC_WRITE"
OP_PUBLIC_EXECUTE = "PUBLIC_EXECUTE"
OP_TOOL_READ = "TOOL_READ"
OP_TOOL_WRITE = "TOOL_WRITE"
OP_TOOL_EXECUTE = "TOOL_EXECUTE"
OP_SYSTEM_READ = "SYSTEM_READ"
OP_SYSTEM_WRITE = "SYSTEM_WRITE"
OP_SYSTEM_EXECUTE = "SYSTEM_EXECUTE"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_op_type(op_type: str | None) -> str:
    key = str(op_type or "").strip().upper()
    if key in _OP_VALUE_MAP:
        return _OP_VALUE_MAP[key]
    return "SYSTEM_EXECUTE"


def _truncate_text(text: Any, *, max_len: int = 4000) -> str:
    value = str(text if text is not None else "")
    if len(value) <= max_len:
        return value
    return value[:max_len]


@dataclass
class _AuditFiles:
    root: Path
    total_jsonl: Path
    total_idx: Path  # {event_id: byte_offset}
    total_text: Path
    error_text: Path
    legacy_error_text: Path


class GlobalAuditEngine:
    """
    用处：全局唯一审计引擎（线程安全、跨重启自增ID、JSONL+文本镜像）。
    """

    def __init__(self, log_root: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._thread_local = threading.local()
        self._failure_count = 0
        self._files = self._build_files(log_root)
        self._counter_file = self._files.root / "id_counter.txt"
        self._current_id = self._load_counter()

    @staticmethod
    def _build_files(log_root: Path | None) -> _AuditFiles:
        if log_root is None:
            root = get_log_root()
        else:
            root = Path(log_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return _AuditFiles(
            root=root,
            total_jsonl=root / "total.jsonl",
            total_idx=root / "total.idx",
            total_text=root / "total.log",
            error_text=root / "error.log",
            legacy_error_text=root / "audit_error.log",
        )

    def _load_counter(self) -> int:
        with self._lock:
            try:
                if not self._counter_file.exists():
                    self._counter_file.write_text("0\n", encoding="utf-8")
                    return 0
                text = self._counter_file.read_text(encoding="utf-8").strip()
                return max(0, int(text or "0"))
            except Exception:
                self._safe_record_error("load_counter_failed", {})
                return 0

    def _persist_counter(self, value: int) -> None:
        temp = self._counter_file.with_name(f"{self._counter_file.name}.tmp")
        temp.write_text(f"{int(value)}\n", encoding="utf-8")
        temp.replace(self._counter_file)

    def next_id(self) -> int:
        with self._lock:
            self._current_id += 1
            try:
                self._persist_counter(self._current_id)
            except Exception:
                # id 仍然在内存中递增，业务继续；错误记录到审计错误日志
                self._safe_record_error("persist_counter_failed", {"id": self._current_id})
            return self._current_id

    def _safe_record_error(self, reason: str, data: dict[str, Any]) -> None:
        self._failure_count += 1
        payload = {
            "ts": _now_iso(),
            "reason": str(reason),
            "data": data,
            "failure_count": self._failure_count,
        }
        line = json.dumps(payload, ensure_ascii=False)
        try:
            with self._files.error_text.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
            # 兼容旧文件名，便于历史工具与脚本继续读取
            with self._files.legacy_error_text.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except Exception:
            # 最后兜底：stderr（不抛异常，不阻断业务）
            try:
                os.write(2, f"[audit-error] {line}\n".encode("utf-8", errors="ignore"))
            except Exception:
                pass

    @staticmethod
    def _subsystem_file_name(subsystem: str) -> str:
        raw = str(subsystem or "system").strip().lower()
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw)
        safe = safe[:64] or "system"
        return f"{safe}.log"

    @staticmethod
    def _format_text_line(event: dict[str, Any]) -> str:
        return (
            f"[{event['id']}] [{event['time']}] [{event['op_type']}] "
            f"[{event['func']}] [{event['dir']}] [{event['file']}] [{event['path']}] {event['content']}"
        )

    def _rotate_if_needed(self) -> None:
        """total.jsonl 超过 10MB 自动归档为 gz。"""
        try:
            size = self._files.total_jsonl.stat().st_size
            if size < 10_485_760:  # 10MB
                return
            now = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive = self._files.root / f"total.{now}.jsonl.gz"
            # 新建空文件替换旧文件，旧文件改名归档
            temp = self._files.root / f"total.jsonl.rotating"
            temp.touch()
            shutil.copy2(str(self._files.total_jsonl), str(self._files.root / f"total.{now}.jsonl"))
            temp.replace(self._files.total_jsonl)
            # 压缩归档
            src = self._files.root / f"total.{now}.jsonl"
            with src.open("rb") as fin, gzip.open(str(archive), "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout)
            src.unlink()
            # 重置索引
            self._files.total_idx.write_text("{}", encoding="utf-8")
        except Exception:
            pass  # 归档失败不阻断业务

    def _write_lines(
        self,
        *,
        json_event: dict[str, Any],
        text_line: str,
        subsystem: str,
    ) -> None:
        self._rotate_if_needed()
        subsystem_file = self._files.root / self._subsystem_file_name(subsystem)
        # 写入 JSONL + 更新索引（先记偏移再写，偏移=写之前文件大小）
        eid = int(json_event["id"])
        idx: dict[int, int] = {}
        try:
            idx_text = self._files.total_idx.read_text(encoding="utf-8")
            idx = json.loads(idx_text) if idx_text.strip() else {}
            if not isinstance(idx, dict):
                idx = {}
        except Exception:
            idx = {}
        offset = self._files.total_jsonl.stat().st_size if self._files.total_jsonl.exists() else 0
        with self._files.total_jsonl.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(json_event, ensure_ascii=False, default=str) + "\n")
        idx[str(eid)] = int(offset)
        # 分摊写入：每 32 条持久化一次索引
        idx.setdefault("_write_count", idx.get("_write_count", 0))
        idx["_write_count"] = int(idx.get("_write_count", 0)) + 1
        try:
            self._files.total_idx.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        # 文本镜像
        with self._files.total_text.open("a", encoding="utf-8") as fp:
            fp.write(text_line + "\n")
        with subsystem_file.open("a", encoding="utf-8") as fp:
            fp.write(text_line + "\n")

    def event(
        self,
        *,
        op_type: str,
        subsystem: str,
        func: str,
        file_path: str,
        content: str,
        extra: dict[str, Any] | None = None,
    ) -> int:
        eid = self.next_id()
        ts = _now_iso()
        path = Path(file_path).resolve()
        record = {
            "id": eid,
            "time": ts,
            "op_type": _normalize_op_type(op_type),
            "subsystem": str(subsystem or "system").strip() or "system",
            "func": str(func or "unknown").strip() or "unknown",
            "dir": str(path.parent.name or ""),
            "file": str(path.name or ""),
            "path": str(path),
            "content": _truncate_text(content, max_len=4000),
        }
        if isinstance(extra, dict) and extra:
            safe_extra: dict[str, Any] = {}
            for k, v in extra.items():
                if k in {"message", "content", "response", "prompt", "history", "token"}:
                    continue
                try:
                    safe_extra[str(k)] = v if isinstance(v, (int, float, bool, dict, list)) else str(v)
                except Exception:
                    safe_extra[str(k)] = str(v)
            if safe_extra:
                record["extra"] = safe_extra

        text_line = self._format_text_line(record)
        try:
            with self._lock:
                self._write_lines(
                    json_event=record,
                    text_line=text_line,
                    subsystem=record["subsystem"],
                )
        except Exception as e:
            self._safe_record_error(
                "write_event_failed",
                {
                    "id": eid,
                    "error": str(e),
                    "subsystem": record["subsystem"],
                    "func": record["func"],
                },
            )
        return eid

    def begin_span(
        self,
        *,
        op_type: str,
        subsystem: str,
        func: str,
        file_path: str,
        content: str,
        extra: dict[str, Any] | None = None,
    ) -> int:
        sid = self.event(
            op_type=op_type,
            subsystem=subsystem,
            func=func,
            file_path=file_path,
            content=content,
            extra=extra,
        )
        stack = getattr(self._thread_local, "stack", None)
        if stack is None:
            stack = []
            self._thread_local.stack = stack
        stack.append(sid)
        return sid

    def end_span(
        self,
        span_id: int | None,
        *,
        op_type: str,
        subsystem: str,
        func: str,
        file_path: str,
        content: str,
        extra: dict[str, Any] | None = None,
    ) -> int:
        eid = self.event(
            op_type=op_type,
            subsystem=subsystem,
            func=func,
            file_path=file_path,
            content=content,
            extra=extra,
        )
        stack = getattr(self._thread_local, "stack", None)
        if isinstance(stack, list) and stack:
            try:
                if span_id is not None and stack[-1] == span_id:
                    stack.pop()
                elif span_id in stack:
                    stack.remove(span_id)
                else:
                    stack.pop()
            except Exception:
                pass
        return eid

    def get_failure_count(self) -> int:
        with self._lock:
            return int(self._failure_count)


_ENGINE_LOCK = threading.Lock()
_ENGINE_SINGLETON: GlobalAuditEngine | None = None


def get_audit_engine() -> GlobalAuditEngine:
    global _ENGINE_SINGLETON
    if _ENGINE_SINGLETON is None:
        with _ENGINE_LOCK:
            if _ENGINE_SINGLETON is None:
                _ENGINE_SINGLETON = GlobalAuditEngine()
    return _ENGINE_SINGLETON


def audit_event(
    *args: Any,
    op_type: str | None = None,
    subsystem: str | None = None,
    func: str | None = None,
    file_path: str | None = None,
    content: str | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    """
    兼容两种调用方式：
    1) 新写法（推荐）：audit_event(op_type=..., subsystem=..., ...)
    2) 旧写法：audit_event(op_type, subsystem, func, file_path, content[, extra])
    """
    if args:
        if len(args) > 6:
            raise TypeError(
                f"audit_event() takes at most 6 positional arguments but {len(args)} were given"
            )
        names = ("op_type", "subsystem", "func", "file_path", "content", "extra")
        for idx, value in enumerate(args):
            name = names[idx]
            if name == "op_type":
                if op_type is not None:
                    raise TypeError("audit_event() got multiple values for argument 'op_type'")
                op_type = str(value)
            elif name == "subsystem":
                if subsystem is not None:
                    raise TypeError("audit_event() got multiple values for argument 'subsystem'")
                subsystem = str(value)
            elif name == "func":
                if func is not None:
                    raise TypeError("audit_event() got multiple values for argument 'func'")
                func = str(value)
            elif name == "file_path":
                if file_path is not None:
                    raise TypeError("audit_event() got multiple values for argument 'file_path'")
                file_path = str(value)
            elif name == "content":
                if content is not None:
                    raise TypeError("audit_event() got multiple values for argument 'content'")
                content = str(value)
            elif name == "extra":
                if extra is not None:
                    raise TypeError("audit_event() got multiple values for argument 'extra'")
                extra = value if isinstance(value, dict) else {"raw_extra": str(value)}

    missing = [
        name
        for name, value in (
            ("op_type", op_type),
            ("subsystem", subsystem),
            ("func", func),
            ("file_path", file_path),
            ("content", content),
        )
        if value is None
    ]
    if missing:
        raise TypeError(f"audit_event() missing required arguments: {', '.join(missing)}")

    return get_audit_engine().event(
        op_type=str(op_type),
        subsystem=str(subsystem),
        func=str(func),
        file_path=str(file_path),
        content=str(content),
        extra=extra,
    )
