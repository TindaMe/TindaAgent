from __future__ import annotations

import os
from pathlib import Path

_TINDA_HOME_ENV = "TINDA_HOME"
_DEFAULT_HOME_PARTS = (".tinda", "agent")


def get_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_runtime_root() -> Path:
    raw = str(os.getenv(_TINDA_HOME_ENV, "")).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / _DEFAULT_HOME_PARTS[0] / _DEFAULT_HOME_PARTS[1]).resolve()


def get_data_root() -> Path:
    return get_runtime_root() / "Data"


def get_log_root() -> Path:
    return get_runtime_root() / "log"


def get_sessions_root() -> Path:
    return get_data_root() / "Sessions"


def get_system_root() -> Path:
    return get_data_root() / "System"


def get_user_root() -> Path:
    return get_data_root() / "User"


def get_users_file() -> Path:
    return get_user_root() / "users.json"


def get_memory_file() -> Path:
    return get_system_root() / "memory.json"


def get_chat_records_root() -> Path:
    return get_data_root() / "ChatRecords"


def get_versions_root() -> Path:
    return get_runtime_root() / "versions"


def get_shared_root() -> Path:
    return get_runtime_root() / "shared"


def get_current_version_file() -> Path:
    return get_runtime_root() / "current.json"


def get_version_trust_root() -> Path:
    return get_runtime_root() / "trust"


def get_version_pubkeys_file() -> Path:
    return get_version_trust_root() / "release_pubkeys.json"


def get_migrations_root() -> Path:
    return get_runtime_root() / "migrations"


def get_schema_state_file() -> Path:
    return get_migrations_root() / "schema.json"


def get_legacy_data_root() -> Path:
    return get_project_root() / "Data"


def get_legacy_log_root() -> Path:
    return get_project_root() / "log"


def get_legacy_sessions_root() -> Path:
    return get_legacy_data_root() / "Sessions"


def get_legacy_users_file() -> Path:
    return get_legacy_data_root() / "User" / "users.json"


def get_legacy_memory_file() -> Path:
    return get_legacy_data_root() / "System" / "memory.json"


def ensure_runtime_dirs() -> None:
    runtime = get_runtime_root()
    (runtime / "Data" / "Sessions").mkdir(parents=True, exist_ok=True)
    (runtime / "Data" / "System").mkdir(parents=True, exist_ok=True)
    (runtime / "Data" / "User").mkdir(parents=True, exist_ok=True)
    (runtime / "log").mkdir(parents=True, exist_ok=True)
    (runtime / "versions").mkdir(parents=True, exist_ok=True)
    (runtime / "shared").mkdir(parents=True, exist_ok=True)
    (runtime / "trust").mkdir(parents=True, exist_ok=True)
    (runtime / "migrations").mkdir(parents=True, exist_ok=True)
