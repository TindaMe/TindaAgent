"""终端显示：颜色、流式输出、确认弹窗。"""


def sanitize_messages(history: list[dict]) -> None:
    """清理 lone surrogate + 保持 reasoning_content 原样（DeepSeek V4 要求）。"""

    def clean_str(s: str) -> str:
        return "".join(c for c in s if ord(c) not in range(0xD800, 0xE000))

    def walk(val):
        if isinstance(val, str):
            return clean_str(val)
        if isinstance(val, dict):
            return {k: walk(v) for k, v in val.items()}
        if isinstance(val, list):
            return [walk(v) for v in val]
        return val

    for m in history:
        for k in list(m.keys()):
            m[k] = walk(m[k])


# ── ANSI 颜色 ──
C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "white": "\033[37m",
}


def c(text: str, color: str) -> str:
    return f"{C.get(color, '')}{text}{C['reset']}"


def _find_pending_in_result(r: dict) -> dict | None:
    for cand in (r, r.get("result") if isinstance(r.get("result"), dict) else None):
        if isinstance(cand, dict) and cand.get("pending_confirmation"):
            return cand
    return None


def print_trace(tool_trace: list[dict]) -> None:
    for step in tool_trace:
        name = step.get("agent_tool", "?")
        r = step.get("result", {})
        if isinstance(r, dict):
            pending = _find_pending_in_result(r)
            if pending:
                note = pending.get("note", "")
                cmd = pending.get("cmd", "?")[:100]
                if note:
                    print(f"  {c(name, 'green')} → ⏸  {c(note, 'cyan')}")
                    print(f"           {c('cmd:', 'dim')} {cmd}")
                else:
                    print(f"  {c(name, 'green')} → ⏸  {cmd}")
                continue
            if r.get("ok") is False:
                print(f"  {c(name, 'green')} → {c('✗', 'red')} {r.get('error', '?')[:80]}")
                continue
        print(f"  {c(name, 'green')} → {c('✓', 'green')}")


def ask_confirm(cmd: str = "", note: str = "") -> bool:
    print()
    if note:
        print(f"  {c('备注:', 'cyan')} {note}")
    print(f"  {c('命令:', 'dim')} {cmd[:120]}")
    print(f"  {c('══════════════════════════════════════', 'dim')}")
    print(f"  {c('[1]', 'green')} 允许执行    {c('[0]', 'red')} 拒绝执行")
    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "0"
    return choice == "1"


def stream_print(text: str, end: str = "") -> None:
    """逐字输出，不换行。"""
    print(text, end=end, flush=True)
