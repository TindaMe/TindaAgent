from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Observability import audit_event

LOGGER = logging.getLogger("tinda.permission")
_TOOL_MIN_PERM_FILE = Path(__file__).resolve().parent / "tool_min_permissions.json"
_THIS_FILE = str(Path(__file__).resolve())

_PERM_MAPPING: tuple[tuple[str, int], ...] = (
    ("PUBLIC_READ", perm.PUBLIC_READ),
    ("PUBLIC_WRITE", perm.PUBLIC_WRITE),
    ("PUBLIC_EXECUTE", perm.PUBLIC_EXECUTE),
    ("TOOL_READ", perm.TOOL_READ),
    ("TOOL_WRITE", perm.TOOL_WRITE),
    ("TOOL_EXECUTE", perm.TOOL_EXECUTE),
    ("SYSTEM_READ", perm.SYSTEM_READ),
    ("SYSTEM_WRITE", perm.SYSTEM_WRITE),
    ("SYSTEM_EXECUTE", perm.SYSTEM_EXECUTE),
)


def has_perm(user_perm: int, required_perm: int) -> bool:
    ok = (int(user_perm) & int(required_perm)) == int(required_perm)
    audit_event(
        op_type="SYSTEM_READ",
        subsystem="permission",
        func="has_perm",
        file_path=_THIS_FILE,
        content="permission_check",
        extra={
            "ok": bool(ok),
            "user_perm": int(user_perm),
            "required_perm": int(required_perm),
        },
    )
    return ok


def perm_labels(value: int) -> list[str]:
    labels: list[str] = []
    p = int(value)
    for name, bit in _PERM_MAPPING:
        if (p & bit) == bit:
            labels.append(name)
    return labels


def missing_perm_labels(user_perm: int, required_perm: int) -> list[str]:
    missing = int(required_perm) & ~int(user_perm)
    return perm_labels(missing)


@lru_cache(maxsize=1)
def _load_tool_min_permissions() -> dict[str, Any]:
    if not _TOOL_MIN_PERM_FILE.exists():
        LOGGER.warning("tool min permission file missing: %s", _TOOL_MIN_PERM_FILE)
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="permission",
            func="_load_tool_min_permissions",
            file_path=_THIS_FILE,
            content="tool_min_permissions_missing",
            extra={"ok": False, "path": str(_TOOL_MIN_PERM_FILE)},
        )
        return {}

    try:
        payload = json.loads(_TOOL_MIN_PERM_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover
        LOGGER.warning("load tool min permission failed: %s", e)
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="permission",
            func="_load_tool_min_permissions",
            file_path=_THIS_FILE,
            content=f"tool_min_permissions_load_failed err={e}",
            extra={"ok": False, "path": str(_TOOL_MIN_PERM_FILE)},
        )
        return {}

    tools = payload.get("tools", {})
    if not isinstance(tools, dict):
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="permission",
            func="_load_tool_min_permissions",
            file_path=_THIS_FILE,
            content="tool_min_permissions_invalid_payload",
            extra={"ok": False, "path": str(_TOOL_MIN_PERM_FILE)},
        )
        return {}
    audit_event(
        op_type="SYSTEM_READ",
        subsystem="permission",
        func="_load_tool_min_permissions",
        file_path=_THIS_FILE,
        content="tool_min_permissions_loaded",
        extra={"ok": True, "path": str(_TOOL_MIN_PERM_FILE), "tool_count": len(tools)},
    )
    return tools


def get_tool_policy(tool_name: str) -> dict[str, Any] | None:
    name = str(tool_name or "").strip()
    if not name:
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="permission",
            func="get_tool_policy",
            file_path=_THIS_FILE,
            content="get_tool_policy_empty_name",
            extra={"ok": False},
        )
        return None
    row = _load_tool_min_permissions().get(name)
    audit_event(
        op_type="SYSTEM_READ",
        subsystem="permission",
        func="get_tool_policy",
        file_path=_THIS_FILE,
        content=f"get_tool_policy tool={name}",
        extra={"ok": isinstance(row, dict), "tool_name": name},
    )
    return row if isinstance(row, dict) else None


def get_tool_required_perm(tool_name: str) -> int | None:
    row = get_tool_policy(tool_name)
    if row is None:
        return None
    try:
        return int(row.get("required_perm_bits"))
    except (TypeError, ValueError):
        return None


def build_permission_denied_payload(tool_name: str, user_perm: int, required_perm: int) -> dict[str, Any]:
    up = int(user_perm)
    rp = int(required_perm)
    user_labels = perm_labels(up)
    req_labels = perm_labels(rp)
    missing_labels = missing_perm_labels(up, rp)

    return {
        "error_code": "permission_denied",
        "tool_name": str(tool_name),
        "required_perm_bits": rp,
        "required_perm_labels": req_labels,
        "user_perm": up,
        "user_perm_labels": user_labels,
        "missing_perm_labels": missing_labels,
        "llm_message": (
            f"调用工具 {tool_name} 失败：权限不足。"
            f"当前权限={up}({ ' | '.join(user_labels) if user_labels else 'NONE' })；"
            f"所需最小权限={rp}({ ' | '.join(req_labels) if req_labels else 'NONE' })；"
            f"缺失权限={', '.join(missing_labels) if missing_labels else 'UNKNOWN'}。"
            "请改用无需该权限的工具，或在不依赖该工具的情况下继续回答。"
        ),
        "user_message": "该工具当前不可用，请尝试其它方式。",
        "expose_to_user": False,
    }


def validate_registered_tool_perm(tool_name: str, declared_perm: int) -> tuple[int, bool]:
    required = get_tool_required_perm(tool_name)
    if required is None:
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="permission",
            func="validate_registered_tool_perm",
            file_path=_THIS_FILE,
            content=f"tool_perm_no_policy tool={tool_name}",
            extra={"tool_name": str(tool_name), "declared_perm": int(declared_perm)},
        )
        return int(declared_perm), False
    declared = int(declared_perm)
    mismatch = required != declared
    if mismatch:
        LOGGER.warning(
            "tool perm mismatch: %s declared=%s policy=%s (policy wins)",
            tool_name,
            declared,
            required,
        )
    audit_event(
        op_type="SYSTEM_READ",
        subsystem="permission",
        func="validate_registered_tool_perm",
        file_path=_THIS_FILE,
        content=f"tool_perm_validated tool={tool_name}",
        extra={
            "tool_name": str(tool_name),
            "declared_perm": declared,
            "required_perm": int(required),
            "mismatch": bool(mismatch),
        },
    )
    return required, mismatch
