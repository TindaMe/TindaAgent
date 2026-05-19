from __future__ import annotations

import contextlib
import io
import json
import queue
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from TindaAgent.Tool import tool as tool_registry
from TindaAgent.Permission.errors import PermissionDeniedError
from TindaAgent.Process.Observability import audit_event

_THIS_FILE = __file__


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ToolJob:
    job_id: str
    session_id: str
    raw_command: str
    status: str
    created_at: str
    updated_at: str
    error: str = ""


class ToolRuntimeManager:
    """
    每个 session 独立一个工具工作线程与队列。
    """

    def __init__(self, *, max_events_per_session: int = 2000) -> None:
        self._max_events = max(200, int(max_events_per_session))
        self._lock = threading.RLock()
        self._queues: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._seq: dict[str, int] = {}
        self._jobs: dict[str, dict[str, ToolJob]] = {}

    def _ensure_session_worker(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._threads and self._threads[session_id].is_alive():
                return
            q: queue.Queue[dict[str, Any]] = self._queues.get(session_id) or queue.Queue()
            self._queues[session_id] = q
            self._events.setdefault(session_id, [])
            self._seq.setdefault(session_id, 0)
            self._jobs.setdefault(session_id, {})
            t = threading.Thread(target=self._worker_main, args=(session_id,), daemon=True, name=f"tool-worker-{session_id}")
            self._threads[session_id] = t
            t.start()

    def submit_command(self, session_id: str, raw_command: str, user_perm: int) -> dict[str, Any]:
        cmd = str(raw_command or "").strip()
        if not cmd.startswith("/"):
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool_runtime",
                func="ToolRuntimeManager.submit_command",
                file_path=_THIS_FILE,
                content="submit_command_rejected_not_slash",
                extra={"session_id": str(session_id), "ok": False},
            )
            raise ValueError("仅支持 / 开头命令")
        self._ensure_session_worker(session_id)

        job_id = f"j_{uuid.uuid4().hex[:14]}"
        now = _now_iso()
        job = ToolJob(
            job_id=job_id,
            session_id=session_id,
            raw_command=cmd,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._jobs[session_id][job_id] = job
            self._queues[session_id].put({"job_id": job_id, "command": cmd, "user_perm": int(user_perm)})
            self._append_event_locked(
                session_id,
                {
                    "type": "job",
                    "status": "queued",
                    "job_id": job_id,
                    "command": cmd,
                    "ts": now,
                },
            )
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool_runtime",
            func="ToolRuntimeManager.submit_command",
            file_path=_THIS_FILE,
            content=f"submit_command_queued job_id={job_id}",
            extra={"session_id": session_id, "job_id": job_id, "ok": True},
        )
        return {
            "job_id": job_id,
            "session_id": session_id,
            "status": "queued",
            "created_at": now,
        }

    def _append_event_locked(self, session_id: str, payload: dict[str, Any]) -> None:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        event = dict(payload)
        event["seq"] = seq
        event["session_id"] = session_id
        event.setdefault("id", f"tool_event_{seq}")
        if str(event.get("type", "") or "") == "terminal":
            event.setdefault("display_target", "terminal")
            event.setdefault("context_policy", "include")
        else:
            event.setdefault("display_target", "terminal")
            event.setdefault("context_policy", "exclude")
        self._events[session_id].append(event)
        overflow = len(self._events[session_id]) - self._max_events
        if overflow > 0:
            del self._events[session_id][:overflow]

    def _set_job_status(self, session_id: str, job_id: str, status: str, error: str = "") -> None:
        with self._lock:
            job = self._jobs.get(session_id, {}).get(job_id)
            if not job:
                return
            job.status = status
            job.updated_at = _now_iso()
            job.error = error
            self._append_event_locked(
                session_id,
                {
                    "type": "job",
                    "status": status,
                    "job_id": job_id,
                    "error": error,
                    "ts": job.updated_at,
                },
            )
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool_runtime",
            func="ToolRuntimeManager._set_job_status",
            file_path=_THIS_FILE,
            content=f"job_status_changed job_id={job_id} status={status}",
            extra={"session_id": session_id, "job_id": job_id, "status": status, "error": error},
        )

    def _worker_main(self, session_id: str) -> None:
        q = self._queues[session_id]
        while True:
            task = q.get()
            if not isinstance(task, dict):
                q.task_done()
                continue
            job_id = str(task.get("job_id", ""))
            cmd = str(task.get("command", ""))
            user_perm = int(task.get("user_perm", 0))
            try:
                self._run_single_job(session_id, job_id, cmd, user_perm)
            except Exception as e:
                self._set_job_status(session_id, job_id, "failed", str(e))
            finally:
                q.task_done()

    def _emit_step(self, session_id: str, job_id: str, kind: str, text: str, *, cls: str = "") -> None:
        with self._lock:
            self._append_event_locked(
                session_id,
                {
                    "type": "terminal",
                    "job_id": job_id,
                    "kind": kind,
                    "text": str(text),
                    "class": cls,
                    "ts": _now_iso(),
                },
            )

    def _run_single_job(self, session_id: str, job_id: str, raw_command: str, user_perm: int) -> None:
        self._set_job_status(session_id, job_id, "running")
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool_runtime",
            func="ToolRuntimeManager._run_single_job",
            file_path=_THIS_FILE,
            content=f"job_start job_id={job_id}",
            extra={"session_id": session_id, "job_id": job_id},
        )

        try:
            parts = shlex.split(raw_command)
        except ValueError as e:
            self._emit_step(session_id, job_id, "out", f"命令解析失败: {e}", cls="err")
            self._set_job_status(session_id, job_id, "failed", f"命令解析失败: {e}")
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool_runtime",
                func="ToolRuntimeManager._run_single_job",
                file_path=_THIS_FILE,
                content=f"job_parse_failed job_id={job_id}",
                extra={"session_id": session_id, "job_id": job_id, "error": str(e), "ok": False},
            )
            return

        if not parts:
            self._emit_step(session_id, job_id, "out", "空命令", cls="err")
            self._set_job_status(session_id, job_id, "failed", "空命令")
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool_runtime",
                func="ToolRuntimeManager._run_single_job",
                file_path=_THIS_FILE,
                content=f"job_empty_command job_id={job_id}",
                extra={"session_id": session_id, "job_id": job_id, "ok": False},
            )
            return

        cmd = parts[0].lower()
        self._emit_step(session_id, job_id, "cmd", raw_command)

        if cmd == "/help":
            self._emit_step(session_id, job_id, "out", "命令列表：/help /tools /tool <name> [args]")
            self._emit_step(session_id, job_id, "sep", "─" * 36)
            self._set_job_status(session_id, job_id, "done")
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool_runtime",
                func="ToolRuntimeManager._run_single_job",
                file_path=_THIS_FILE,
                content=f"job_help_done job_id={job_id}",
                extra={"session_id": session_id, "job_id": job_id, "ok": True},
            )
            return

        if cmd == "/tools":
            tools = tool_registry.list_tools(user_perm)
            if not tools:
                self._emit_step(session_id, job_id, "out", "当前权限下没有可用工具。")
            else:
                self._emit_step(session_id, job_id, "out", "可用工具：", cls="info")
                for name, desc in sorted(tools.items()):
                    self._emit_step(session_id, job_id, "out", f"- {name}: {desc}")
            self._emit_step(session_id, job_id, "sep", "─" * 36)
            self._set_job_status(session_id, job_id, "done")
            audit_event(
                op_type="TOOL_READ",
                subsystem="tool_runtime",
                func="ToolRuntimeManager._run_single_job",
                file_path=_THIS_FILE,
                content=f"job_tools_done job_id={job_id}",
                extra={"session_id": session_id, "job_id": job_id, "ok": True},
            )
            return

        if cmd != "/tool":
            self._emit_step(session_id, job_id, "out", "未知命令。输入 /help 查看可用命令。", cls="err")
            self._emit_step(session_id, job_id, "sep", "─" * 36)
            self._set_job_status(session_id, job_id, "failed", "unknown command")
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool_runtime",
                func="ToolRuntimeManager._run_single_job",
                file_path=_THIS_FILE,
                content=f"job_unknown_command job_id={job_id}",
                extra={"session_id": session_id, "job_id": job_id, "ok": False, "command": cmd},
            )
            return

        if len(parts) < 2:
            self._emit_step(session_id, job_id, "out", "用法: /tool <工具名> [参数...]", cls="err")
            self._emit_step(session_id, job_id, "sep", "─" * 36)
            self._set_job_status(session_id, job_id, "failed", "missing tool name")
            audit_event(
                op_type="TOOL_EXECUTE",
                subsystem="tool_runtime",
                func="ToolRuntimeManager._run_single_job",
                file_path=_THIS_FILE,
                content=f"job_missing_tool_name job_id={job_id}",
                extra={"session_id": session_id, "job_id": job_id, "ok": False},
            )
            return

        tool_name = parts[1]
        args = parts[2:]

        capture = io.StringIO()
        result: Any = None
        with contextlib.redirect_stdout(capture):
            try:
                result = tool_registry.run_tool(tool_name, user_perm, *args)
            except (ValueError, PermissionError, PermissionDeniedError) as e:
                self._emit_step(session_id, job_id, "out", str(e), cls="err")
                self._emit_step(session_id, job_id, "sep", "─" * 36)
                self._set_job_status(session_id, job_id, "failed", str(e))
                audit_event(
                    op_type="TOOL_EXECUTE",
                    subsystem="tool_runtime",
                    func="ToolRuntimeManager._run_single_job",
                    file_path=_THIS_FILE,
                    content=f"job_tool_failed job_id={job_id} tool={tool_name}",
                    extra={"session_id": session_id, "job_id": job_id, "tool_name": tool_name, "ok": False, "error": str(e)},
                )
                return

        # 检测终端命令挂起确认
        pending = False
        if isinstance(result, dict) and result.get("pending_confirmation") is True:
            pending = True
            cid = str(result.get("confirm_id", ""))
            with self._lock:
                self._append_event_locked(
                    session_id,
                    {
                        "type": "terminal_confirm",
                        "job_id": job_id,
                        "confirm_id": cid,
                        "cmd": str(result.get("cmd", "")),
                        "status": "pending",
                        "ts": _now_iso(),
                    },
                )
            self._emit_step(session_id, job_id, "out", f"[warn] 等待确认: {str(result.get('cmd', ''))[:80]}", cls="info")
            self._set_job_status(session_id, job_id, "pending_confirm")
            return

        printed = capture.getvalue().strip()
        self._emit_step(session_id, job_id, "out", f"tool: {tool_name}", cls="info")
        if printed:
            for line in printed.split("\n"):
                self._emit_step(session_id, job_id, "out", line)
        if result is not None:
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            for line in str(text).split("\n"):
                self._emit_step(session_id, job_id, "out", line)
        if not printed and result is None:
            self._emit_step(session_id, job_id, "out", "工具执行完成。")

        self._emit_step(session_id, job_id, "sep", "─" * 36)
        self._set_job_status(session_id, job_id, "done")
        audit_event(
            op_type="TOOL_EXECUTE",
            subsystem="tool_runtime",
            func="ToolRuntimeManager._run_single_job",
            file_path=_THIS_FILE,
            content=f"job_tool_done job_id={job_id} tool={tool_name}",
            extra={"session_id": session_id, "job_id": job_id, "tool_name": tool_name, "ok": True},
        )

    def get_events(self, session_id: str, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        after = max(0, int(after_seq))
        lim = max(1, min(int(limit), 1000))
        with self._lock:
            rows = list(self._events.get(session_id, []))
            filtered = [e for e in rows if int(e.get("seq", 0)) > after]
            paged = filtered[:lim]
            next_seq = after
            if paged:
                next_seq = int(paged[-1].get("seq", after))
            elif rows:
                next_seq = int(rows[-1].get("seq", after))
            return {
                "session_id": session_id,
                "after_seq": after,
                "events": paged,
                "next_seq": next_seq,
                "total": len(filtered),
            }

    def get_job(self, session_id: str, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(session_id, {}).get(job_id)
            if job is None:
                return None
            return {
                "job_id": job.job_id,
                "session_id": job.session_id,
                "raw_command": job.raw_command,
                "status": job.status,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "error": job.error,
            }
