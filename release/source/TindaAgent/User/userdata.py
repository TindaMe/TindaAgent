from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from TindaAgent.Process.Architecture import perm
from TindaAgent.Process.Architecture.paths import get_users_file
from TindaAgent.Process.Observability import audit_event

_user_registry: list["UserManager"] = []
_USERS_FILE = get_users_file()
_THIS_FILE = str(Path(__file__).resolve())


def _ensure_data_dir() -> None:
    _USERS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _user_to_dict(user: "UserManager") -> dict[str, Any]:
    return {
        "uid": user.uid,
        "name": user.name,
        "perm": user.perm,
        "token": user.token,
    }


def _persist_users() -> None:
    _ensure_data_dir()
    payload = {
        "next_uid": UserManager._uid + 1,
        "users": [_user_to_dict(u) for u in _user_registry],
    }
    _USERS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    audit_event(
        op_type="SYSTEM_WRITE",
        subsystem="user",
        func="_persist_users",
        file_path=_THIS_FILE,
        content="persist_users",
        extra={"users_count": len(_user_registry), "path": str(_USERS_FILE)},
    )


def _build_user(
    username: str,
    userperm: int,
    usertoken: str,
    uid: str | None = None,
    persist: bool = True,
) -> "UserManager":
    obj = UserManager.__new__(UserManager)
    if uid is None:
        UserManager._uid += 1
        obj.uid = str(UserManager._uid).zfill(10)
    else:
        uid_int = max(1, _safe_int(uid, 1))
        obj.uid = str(uid_int).zfill(10)
        if uid_int > UserManager._uid:
            UserManager._uid = uid_int
    obj.name = username
    obj.perm = userperm
    obj.token = usertoken
    _user_registry.append(obj)
    if persist:
        _persist_users()
    return obj


def _load_users_from_disk() -> None:
    _ensure_data_dir()
    if not _USERS_FILE.exists():
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="user",
            func="_load_users_from_disk",
            file_path=_THIS_FILE,
            content="users_file_not_found",
            extra={"path": str(_USERS_FILE)},
        )
        return

    try:
        raw = _USERS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="user",
            func="_load_users_from_disk",
            file_path=_THIS_FILE,
            content="users_file_parse_failed",
            extra={"path": str(_USERS_FILE), "ok": False},
        )
        return

    users_data = data.get("users", [])
    if not isinstance(users_data, list):
        users_data = []

    _user_registry.clear()
    UserManager._uid = 0
    for item in users_data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        token = str(item.get("token", "")).strip() or secrets.token_hex(32)
        perm_value = _safe_int(item.get("perm"), perm.USER_VISITOR)
        uid_value = str(item.get("uid", "")).strip()
        _build_user(
            username=name,
            userperm=perm_value,
            usertoken=token,
            uid=uid_value or None,
            persist=False,
        )

    next_uid = _safe_int(data.get("next_uid"), UserManager._uid + 1)
    if next_uid > UserManager._uid:
        UserManager._uid = next_uid - 1
    audit_event(
        op_type="SYSTEM_READ",
        subsystem="user",
        func="_load_users_from_disk",
        file_path=_THIS_FILE,
        content="users_loaded",
        extra={"users_count": len(_user_registry), "next_uid": UserManager._uid + 1},
    )


def _ensure_seed_user() -> None:
    if any(u.name == "Tinda" for u in _user_registry):
        audit_event(
            op_type="SYSTEM_READ",
            subsystem="user",
            func="_ensure_seed_user",
            file_path=_THIS_FILE,
            content="seed_user_exists",
            extra={"name": "Tinda"},
        )
        return
    _build_user(
        username="Tinda",
        userperm=perm.USER_ADMIN,
        usertoken=secrets.token_hex(32),
        persist=False,
    )
    _persist_users()
    audit_event(
        op_type="SYSTEM_WRITE",
        subsystem="user",
        func="_ensure_seed_user",
        file_path=_THIS_FILE,
        content="seed_user_created",
        extra={"name": "Tinda"},
    )


def get_user_from_name(name: str) -> UserManager | None:
    for user in _user_registry:
        if user.get_name() == name:
            return user
    return None


def get_user_from_uid(uid: str) -> UserManager | None:
    for user in _user_registry:
        if user.get_uid() == uid:
            return user
    return None


def get_user_from_token(token: str) -> UserManager | None:
    needle = str(token or "").strip()
    if not needle:
        return None
    for user in _user_registry:
        try:
            if str(user.get_token()) == needle:
                return user
        except Exception:
            continue
    return None


def ensure_default_user(name: str = "Tinda") -> "UserManager":
    user = get_user_from_name(name)
    if user is not None:
        return user
    return _build_user(
        username=name,
        userperm=perm.USER_ADMIN,
        usertoken=secrets.token_hex(32),
        persist=True,
    )


def ensure_zero_perm_user(name: str = "guest0") -> "UserManager":
    """
    用处：确保存在一个 0 权限测试用户
    """
    # 只要已经存在任意 0 权限用户，就不再强制补建指定名称，避免改名后自动“再生”新账号。
    for u in _user_registry:
        try:
            if int(u.get_perm()) == 0:
                return u
        except Exception:
            continue

    user = get_user_from_name(name)
    if user is not None:
        if int(user.get_perm()) != 0:
            user.change_perm(0)
        return user
    return _build_user(
        username=name,
        userperm=0,
        usertoken=secrets.token_hex(32),
        persist=True,
    )


def _normalize_uid(uid: str) -> str:
    text = str(uid or "").strip()
    if not text:
        return ""
    try:
        return str(max(1, int(text))).zfill(10)
    except Exception:
        return text


def user_name_exists(name: str, *, exclude_uid: str | None = None) -> bool:
    check = str(name or "").strip()
    if not check:
        return False
    exclude = _normalize_uid(exclude_uid or "")
    for u in _user_registry:
        if exclude and u.get_uid() == exclude:
            continue
        if u.get_name() == check:
            return True
    return False


def _ensure_manage_users_perm(actor: "UserManager" | None) -> None:
    """
    用处：底层防线，所有用户增删改操作都必须由满权限账号发起。
    """
    if actor is None:
        audit_event(
            op_type="SYSTEM_EXECUTE",
            subsystem="user",
            func="_ensure_manage_users_perm",
            file_path=_THIS_FILE,
            content="manage_users_denied_no_actor",
            extra={"ok": False},
        )
        raise PermissionError("permission denied")
    actor_perm = _safe_int(actor.get_perm(), 0)
    required = int(perm.USER_ADMIN)
    if (actor_perm & required) != required:
        audit_event(
            op_type="SYSTEM_EXECUTE",
            subsystem="user",
            func="_ensure_manage_users_perm",
            file_path=_THIS_FILE,
            content="manage_users_denied_perm",
            extra={
                "ok": False,
                "actor_uid": str(actor.get_uid()),
                "actor_perm": actor_perm,
                "required_perm": required,
            },
        )
        raise PermissionError("permission denied")
    audit_event(
        op_type="SYSTEM_EXECUTE",
        subsystem="user",
        func="_ensure_manage_users_perm",
        file_path=_THIS_FILE,
        content="manage_users_allowed",
        extra={"ok": True, "actor_uid": str(actor.get_uid()), "actor_perm": actor_perm},
    )


def create_user(
    name: str,
    userperm: int,
    usertoken: str | None = None,
    *,
    actor: "UserManager" | None = None,
) -> "UserManager":
    _ensure_manage_users_perm(actor)
    uname = str(name or "").strip()
    if not uname:
        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="user",
            func="create_user",
            file_path=_THIS_FILE,
            content="create_user_invalid_empty_name",
            extra={"ok": False},
        )
        raise ValueError("用户名不能为空")
    if user_name_exists(uname):
        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="user",
            func="create_user",
            file_path=_THIS_FILE,
            content=f"create_user_name_exists name={uname}",
            extra={"ok": False, "name": uname},
        )
        raise ValueError("用户名已存在")
    token = str(usertoken or "").strip() or secrets.token_hex(32)
    created = _build_user(
        username=uname,
        userperm=int(userperm),
        usertoken=token,
        persist=True,
    )
    audit_event(
        op_type="SYSTEM_WRITE",
        subsystem="user",
        func="create_user",
        file_path=_THIS_FILE,
        content=f"create_user_done uid={created.get_uid()}",
        extra={
            "ok": True,
            "uid": str(created.get_uid()),
            "name": str(created.get_name()),
            "perm": int(created.get_perm()),
        },
    )
    return created


def update_user(
    uid: str,
    *,
    name: str | None = None,
    userperm: int | None = None,
    usertoken: str | None = None,
    actor: "UserManager" | None = None,
) -> "UserManager" | None:
    _ensure_manage_users_perm(actor)
    target = get_user_from_uid(_normalize_uid(uid))
    if target is None:
        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="user",
            func="update_user",
            file_path=_THIS_FILE,
            content=f"update_user_not_found uid={uid}",
            extra={"ok": False, "uid": str(uid)},
        )
        return None
    if name is not None:
        new_name = str(name).strip()
        if not new_name:
            raise ValueError("用户名不能为空")
        if user_name_exists(new_name, exclude_uid=target.get_uid()):
            raise ValueError("用户名已存在")
        target.name = new_name
    if userperm is not None:
        target.perm = int(userperm)
    if usertoken is not None:
        new_token = str(usertoken).strip()
        if not new_token:
            raise ValueError("token 不能为空")
        target.token = new_token
    _persist_users()
    audit_event(
        op_type="SYSTEM_WRITE",
        subsystem="user",
        func="update_user",
        file_path=_THIS_FILE,
        content=f"update_user_done uid={target.get_uid()}",
        extra={
            "ok": True,
            "uid": str(target.get_uid()),
            "name": str(target.get_name()),
            "perm": int(target.get_perm()),
            "token_changed": usertoken is not None,
        },
    )
    return target


def delete_user(uid: str, *, actor: "UserManager" | None = None) -> bool:
    _ensure_manage_users_perm(actor)
    key = _normalize_uid(uid)
    for idx, user in enumerate(_user_registry):
        if user.get_uid() == key:
            _user_registry.pop(idx)
            _persist_users()
            audit_event(
                op_type="SYSTEM_WRITE",
                subsystem="user",
                func="delete_user",
                file_path=_THIS_FILE,
                content=f"delete_user_done uid={key}",
                extra={"ok": True, "uid": key},
            )
            return True
    audit_event(
        op_type="SYSTEM_WRITE",
        subsystem="user",
        func="delete_user",
        file_path=_THIS_FILE,
        content=f"delete_user_not_found uid={key}",
        extra={"ok": False, "uid": key},
    )
    return False


def is_system_user(user: "UserManager" | None) -> bool:
    """
    用处：判断是否为系统内部用户（不对外展示）
    """
    if user is None:
        return False
    name = str(user.get_name() or "")
    return name.startswith("web-bot-")


def cleanup_system_users(*, persist: bool = True) -> int:
    """
    用处：清理历史遗留的系统内部用户（web-bot-*）
    返回：被清理数量
    """
    global _user_registry
    before = len(_user_registry)
    _user_registry = [u for u in _user_registry if not is_system_user(u)]
    removed = before - len(_user_registry)
    if removed > 0 and persist:
        _persist_users()
    audit_event(
        op_type="SYSTEM_WRITE",
        subsystem="user",
        func="cleanup_system_users",
        file_path=_THIS_FILE,
        content=f"cleanup_system_users removed={removed}",
        extra={"removed": removed, "persist": bool(persist)},
    )
    return removed


class UserManager:
    # 用户ID常数（自增）
    _uid = 0

    def __init__(
        self,
        username: str,
        userperm: int,
        usertoken: str | None = None,
        persist: bool = True,
    ) -> None:
        """
        用处： 初始化用户管理器

        参数：
            username: str // 用户名
            userperm: int // 用户权限
            usertoken: str // 用户令牌（可选，默认生成随机令牌）
            persist: bool // 是否写入用户注册表并落盘
        """
        UserManager._uid += 1
        self.uid = str(UserManager._uid).zfill(10)
        self.name = username
        self.perm = userperm
        self.token = usertoken or secrets.token_hex(32)
        if persist:
            _user_registry.append(self)
            _persist_users()

    def change_name(self, new_name: str) -> None:
        self.name = new_name
        _persist_users()
        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="user",
            func="UserManager.change_name",
            file_path=_THIS_FILE,
            content=f"user_change_name uid={self.uid}",
            extra={"uid": self.uid, "name": self.name},
        )

    def change_perm(self, new_perm: int) -> None:
        self.perm = new_perm
        _persist_users()
        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="user",
            func="UserManager.change_perm",
            file_path=_THIS_FILE,
            content=f"user_change_perm uid={self.uid}",
            extra={"uid": self.uid, "perm": int(self.perm)},
        )

    def change_token(self, new_token: str) -> None:
        self.token = new_token
        _persist_users()
        audit_event(
            op_type="SYSTEM_WRITE",
            subsystem="user",
            func="UserManager.change_token",
            file_path=_THIS_FILE,
            content=f"user_change_token uid={self.uid}",
            extra={"uid": self.uid},
        )

    def get_uid(self) -> str:
        return self.uid

    def get_name(self) -> str:
        return self.name

    def get_perm(self) -> int:
        return self.perm

    def get_token(self) -> str:
        return self.token


def iter_users() -> list[UserManager]:
    """
    用处：返回当前注册用户快照
    """
    return list(_user_registry)


def export_public_user(user: UserManager | None) -> dict[str, Any]:
    if user is None:
        return {"name": "", "uid": "", "perm": 0, "token": ""}
    return {
        "name": user.get_name(),
        "uid": user.get_uid(),
        "perm": user.get_perm(),
        "token": user.get_token(),
    }


# 模块加载时恢复数据并确保默认测试用户存在
_load_users_from_disk()
_ensure_seed_user()
ensure_zero_perm_user("guest0")
