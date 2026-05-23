from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from TindaAgent.Process.Architecture.paths import get_runtime_root

MCP_CONFIG_FILE = get_runtime_root() / "mcp" / "servers.json"
MCP_PROTOCOL_VERSION = "2025-06-18"
_ACTIVE_MCP_PROCS: dict[str, subprocess.Popen[str]] = {}
_ACTIVE_MCP_LOCK = threading.RLock()


class McpError(RuntimeError):
    pass


def _default_config() -> dict[str, Any]:
    return {"version": 1, "servers": {}}


def load_mcp_config() -> dict[str, Any]:
    try:
        if not MCP_CONFIG_FILE.exists():
            return _default_config()
        data = json.loads(MCP_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_config()
        servers = data.get("servers")
        if not isinstance(servers, dict):
            data["servers"] = {}
        data.setdefault("version", 1)
        return data
    except Exception:
        return _default_config()


def save_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    payload = config if isinstance(config, dict) else _default_config()
    payload.setdefault("version", 1)
    payload.setdefault("servers", {})
    MCP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MCP_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(MCP_CONFIG_FILE)
    return payload


def upsert_mcp_server(name: str, command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    server_name = str(name or "").strip()
    if not server_name:
        raise ValueError("name is required")
    cmd = str(command or "").strip()
    if not cmd:
        raise ValueError("command is required")
    config = load_mcp_config()
    servers = config.setdefault("servers", {})
    servers[server_name] = {
        "command": cmd,
        "args": [str(x) for x in (args or [])],
        "env": {str(k): str(v) for k, v in (env or {}).items()},
    }
    save_mcp_config(config)
    return {"ok": True, "server": server_name, "config_file": str(MCP_CONFIG_FILE)}


def list_mcp_servers() -> dict[str, Any]:
    config = load_mcp_config()
    servers = []
    for name, row in sorted((config.get("servers") or {}).items()):
        if not isinstance(row, dict):
            continue
        servers.append({
            "name": str(name),
            "command": str(row.get("command") or ""),
            "args": [str(x) for x in row.get("args", []) if isinstance(x, (str, int, float))],
        })
    return {"ok": True, "config_file": str(MCP_CONFIG_FILE), "servers": servers}


def cancel_mcp_call(call_id: str) -> bool:
    cid = str(call_id or "").strip()
    if not cid:
        return False
    with _ACTIVE_MCP_LOCK:
        proc = _ACTIVE_MCP_PROCS.get(cid)
    if proc is None:
        return False
    try:
        if hasattr(os, "killpg") and int(getattr(proc, "pid", 0) or 0) > 0:
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        return True
    except Exception:
        try:
            proc.kill()
            return True
        except Exception:
            return False


class StdioMcpClient:
    def __init__(self, server: dict[str, Any], *, timeout: float = 15.0, call_id: str = "") -> None:
        self.server = server
        self.timeout = float(timeout)
        self.call_id = str(call_id or "").strip()
        self._id = 0
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()

    def __enter__(self) -> "StdioMcpClient":
        cmd = str(self.server.get("command") or "").strip()
        if not cmd:
            raise McpError("MCP server command missing")
        args = [str(x) for x in self.server.get("args", [])]
        env = None
        if isinstance(self.server.get("env"), dict):
            import os
            env = {**os.environ, **{str(k): str(v) for k, v in self.server.get("env", {}).items()}}
        self._proc = subprocess.Popen(
            [cmd, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        if self.call_id:
            with _ACTIVE_MCP_LOCK:
                _ACTIVE_MCP_PROCS[self.call_id] = self._proc
        try:
            self.request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "TindaAgent", "version": "1"},
            })
            self.notify("notifications/initialized", {})
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        proc = self._proc
        if self.call_id:
            with _ACTIVE_MCP_LOCK:
                _ACTIVE_MCP_PROCS.pop(self.call_id, None)
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        req_id = self._next_id()
        self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + max(1.0, float(self.timeout))
        while True:
            msg = self._read(deadline=deadline)
            if msg.get("id") != req_id:
                continue
            if isinstance(msg.get("error"), dict):
                err = msg["error"]
                raise McpError(str(err.get("message") or err))
            return msg.get("result")

    def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError("MCP process not started")
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _read(self, *, deadline: float) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise McpError("MCP process not started")
        remaining = max(0.0, float(deadline) - time.monotonic())
        if remaining <= 0:
            raise McpError("MCP request timed out")
        try:
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
        except Exception:
            ready = [proc.stdout]
        if not ready:
            raise McpError("MCP request timed out")
        line = proc.stdout.readline()
        if not line:
            stderr = ""
            try:
                stderr = proc.stderr.read(4000) if proc.stderr else ""
            except Exception:
                pass
            raise McpError(f"MCP server closed stdout. {stderr}".strip())
        data = json.loads(line)
        if not isinstance(data, dict):
            raise McpError("invalid MCP response")
        return data


def _server_config(name: str) -> dict[str, Any]:
    server_name = str(name or "").strip()
    config = load_mcp_config()
    row = (config.get("servers") or {}).get(server_name)
    if not isinstance(row, dict):
        raise McpError(f"MCP server not configured: {server_name}")
    return row


def list_mcp_tools(server: str) -> dict[str, Any]:
    with StdioMcpClient(_server_config(server)) as client:
        result = client.request("tools/list", {})
    tools = result.get("tools", []) if isinstance(result, dict) else []
    return {"ok": True, "server": str(server), "tools": tools if isinstance(tools, list) else []}


def call_mcp_tool(server: str, tool_name: str, arguments: dict[str, Any] | None = None, *, call_id: str = "") -> dict[str, Any]:
    with StdioMcpClient(_server_config(server), timeout=60, call_id=call_id) as client:
        result = client.request("tools/call", {
            "name": str(tool_name or "").strip(),
            "arguments": arguments if isinstance(arguments, dict) else {},
        })
    return {"ok": True, "server": str(server), "tool_name": str(tool_name), "result": result}
