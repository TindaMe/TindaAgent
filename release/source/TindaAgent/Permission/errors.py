from __future__ import annotations


class PermissionDeniedError(PermissionError):
    """权限不足错误，支持携带结构化 payload。"""

    def __init__(self, message: str = "权限不足", *, payload: dict | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}
