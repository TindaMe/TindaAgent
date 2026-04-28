"""
轻量 token 估算（无外部依赖）。英文 ~4 字符/token，CJK ~1.5 字符/token。
"""


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    s = str(text)
    non_ascii = sum(1 for c in s if ord(c) > 127)
    ascii_chars = len(s) - non_ascii
    return max(1, int(ascii_chars / 4.0 + non_ascii / 1.5))


def estimate_message_tokens(msg: dict) -> int:
    total = 4  # role + overhead
    content = msg.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                if isinstance(fn, dict):
                    total += estimate_tokens(fn.get("name", ""))
                    total += estimate_tokens(fn.get("arguments", ""))
    # tool-call result
    if msg.get("role") == "tool" and isinstance(content, str):
        total += 2  # tool_call_id overhead
    return total


def estimate_messages_tokens(messages: list[dict]) -> int:
    if not messages:
        return 0
    return sum(estimate_message_tokens(m) for m in messages)


def format_token_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)
