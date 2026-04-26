from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from TindaAgent.Permission import has_perm as _has_perm


@dataclass(frozen=True)
class SecurityPrincipal:
    uid: str
    name: str
    perm: int
    token: str


_CURRENT_PRINCIPAL: ContextVar[SecurityPrincipal | None] = ContextVar(
    "tinda_security_principal",
    default=None,
)
_CURRENT_USER: ContextVar[Any | None] = ContextVar(
    "tinda_security_user",
    default=None,
)


def _to_principal(user: Any | None) -> SecurityPrincipal | None:
    if user is None:
        return None
    try:
        return SecurityPrincipal(
            uid=str(user.get_uid()),
            name=str(user.get_name()),
            perm=int(user.get_perm()),
            token=str(user.get_token()),
        )
    except Exception:
        return None


def push_current_user(user: Any | None) -> tuple[Token, Token]:
    principal_token = _CURRENT_PRINCIPAL.set(_to_principal(user))
    user_token = _CURRENT_USER.set(user)
    return principal_token, user_token


def reset_current_user(tokens: tuple[Token, Token]) -> None:
    principal_token, user_token = tokens
    _CURRENT_PRINCIPAL.reset(principal_token)
    _CURRENT_USER.reset(user_token)


def clear_current_user() -> None:
    _CURRENT_PRINCIPAL.set(None)
    _CURRENT_USER.set(None)


def get_current_principal() -> SecurityPrincipal | None:
    return _CURRENT_PRINCIPAL.get()


def get_current_user() -> Any | None:
    return _CURRENT_USER.get()


def require_user() -> Any:
    user = get_current_user()
    if user is None:
        raise RuntimeError("not logged in")
    return user


def current_perm() -> int:
    principal = get_current_principal()
    if principal is None:
        return 0
    return int(principal.perm)


def has_perm(required_perm: int) -> bool:
    return _has_perm(current_perm(), int(required_perm))
