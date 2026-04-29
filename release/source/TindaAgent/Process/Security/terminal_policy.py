"""
终端命令执行策略：白名单/黑名单 + 系统操作检测 + bypass逻辑。
"""

import re
from pathlib import Path
from typing import Any

DEFAULT_WHITELIST: list[str] = []
DEFAULT_BLACKLIST: list[str] = [
    "rm -rf /", "rm -rf ~", "rm -rf .", "dd if=", "mkfs.",
    "> /dev/sda", ":(){ :|:& };:", "chmod 777 /",
    "wget", "curl",  # 网络下载需用户显式放行
]

_SYS_CMD_PATTERNS = [
    (re.compile(p, re.IGNORECASE), label)
    for p, label in [
        (r"\brm\b", "rm"),
        (r"\bmv\b", "mv"),
        (r"\bdel\b", "del"),
        (r"\bformat\b", "format"),
        (r"\bdd\b", "dd"),
        (r"\bmkfs\.", "mkfs"),
        (r"\bchmod\b", "chmod"),
        (r"\bchown\b", "chown"),
        (r"\bmount\b", "mount"),
        (r"\bumount\b", "umount"),
        (r"\bfdisk\b", "fdisk"),
        (r"\bparted\b", "parted"),
        (r"\bshutdown\b", "shutdown"),
        (r"\breboot\b", "reboot"),
        (r"\biptables\b", "iptables"),
        (r"\bsystemctl\b", "systemctl"),
        (r"\bsudo\b", "sudo"),
        (r"\bsu\b", "su"),
        (r"\bpasswd\b", "passwd"),
        (r"\bkill\b", "kill"),
        (r"\bpkill\b", "pkill"),
        (r"\bkillall\b", "killall"),
        (r"\bgit\s+push", "git-push"),
        (r"\bdocker\b", "docker"),
    ]
]


def load_settings() -> dict[str, Any]:
    path = Path("~/.tinda/agent/terminal_settings.json").expanduser()
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_settings(data: dict[str, Any]) -> None:
    import json
    path = Path("~/.tinda/agent/terminal_settings.json").expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_whitelist() -> list[str]:
    s = load_settings()
    return [str(x).strip() for x in s.get("whitelist", DEFAULT_WHITELIST) if str(x).strip()]


def get_blacklist() -> list[str]:
    s = load_settings()
    return [str(x).strip() for x in s.get("blacklist", DEFAULT_BLACKLIST) if str(x).strip()]


def is_bypass_enabled(user_perm: int) -> bool:
    if (user_perm & 511) != 511:
        return False
    s = load_settings()
    return bool(s.get("bypass_terminal_confirm", False))


def check_blacklist(command: str) -> list[str]:
    """返回匹配的黑名单项（空列表表示通过）。"""
    lower = command.lower().strip()
    blocked = []
    for pattern in get_blacklist():
        if pattern.lower() in lower:
            blocked.append(pattern)
    return blocked


def check_whitelist(command: str) -> bool:
    """命令是否命中白名单。"""
    white = get_whitelist()
    if not white:
        return False
    lower = command.lower().strip()
    for pattern in white:
        if pattern.lower() in lower:
            return True
    return False


def detect_system_operations(command: str) -> list[str]:
    """检测命令中的系统级操作（返回匹配的标签列表）。"""
    hits: list[str] = []
    for pattern, label in _SYS_CMD_PATTERNS:
        if pattern.search(command):
            hits.append(label)
    return list(set(hits))


def needs_system_perm(command: str) -> bool:
    return len(detect_system_operations(command)) > 0
