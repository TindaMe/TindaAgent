from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from TindaAgent.Process.Architecture import perm

_user_registry: list["UserManager"] = []
_USERS_FILE = Path(__file__).resolve().parent.parent / "Data" / "User" / "users.json"


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
        return

    try:
        raw = _USERS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
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


def _ensure_seed_user() -> None:
    if any(u.name == "Tinda" for u in _user_registry):
        return
    _build_user(
        username="Tinda",
        userperm=perm.USER_ADMIN,
        usertoken=secrets.token_hex(32),
        persist=False,
    )
    _persist_users()


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

    def change_perm(self, new_perm: int) -> None:
        self.perm = new_perm
        _persist_users()

    def change_token(self, new_token: str) -> None:
        self.token = new_token
        _persist_users()

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
