#!/usr/bin/env python3
"""Convert existing session JSONL to new format, overwrite as .json"""

import json, sys
from datetime import datetime
from pathlib import Path
from collections import OrderedDict

SRC = Path("/mnt/e/.tinda/agent/Data/Sessions/messages/s_ce93941d0f3a.jsonl")
DST = SRC.with_suffix(".json")

# Load old messages
old_msgs = []
with open(SRC) as f:
    for line in f:
        line = line.strip()
        if line:
            old_msgs.append(json.loads(line))

print(f"Loaded {len(old_msgs)} old messages from {SRC.name}")

# Group by turn: collect tool_marker + terminal entries that belong to each assistant message
records = OrderedDict()
seq = 0
seen_users = 0
pending_terminal = []  # terminal out/sep without turn_id

for m in old_msgs:
    role = m.get("role", "")
    et = m.get("entry_type", "chat")
    content = m.get("content", "")
    turn_id = m.get("turn_id", "")
    term_kind = m.get("terminal_kind", "")
    reasoning = m.get("reasoning_content", "")
    old_id = m.get("id", "")

    if et == "terminal" and term_kind == "sep":
        continue  # skip separators

    if et == "chat" and role == "user":
        seq += 1
        is_first = (seen_users == 0)
        seen_users += 1
        c = {"user": content} if is_first else {"text": content}
        records[str(seq)] = {
            "role": "user",
            "id": _fmt_id(),
            "content": c,
        }

    elif et == "chat" and role == "assistant":
        seq += 1
        substeps = OrderedDict()
        sn = 0
        if reasoning:
            sn += 1
            substeps[str(sn)] = {"thinking": reasoning}
        if content.strip():
            sn += 1
            substeps[str(sn)] = {"text": content}
        records[str(seq)] = {
            "role": "assistant",
            "id": _fmt_id(),
            "content": substeps if substeps else {"text": ""},
        }

    elif et == "tool_marker":
        # Parse call_id from content: "> >_<\n> --调用工具中--\n> #tc_xxx"
        cid = ""
        for line in content.split("\n"):
            if line.startswith("> #tc_"):
                cid = line.replace("> #tc_", "").strip()
                break
            if line.startswith("> id: tc_"):
                cid = line.replace("> id: tc_", "").strip()
                break
        # Find matching terminal cmd to get tool_name
        tool_name = "unknown"
        stdin = ""
        stdout = ""
        ok = False
        # Look for next terminal entries that might belong to this tool
        # (in the same turn, or immediately after without turn_id)

        pending_terminal.append({
            "seq": seq,
            "call_id": cid,
            "tool_name": tool_name,
            "ok": ok,
            "stdin": stdin,
            "stdout": stdout,
        })

    elif et == "terminal" and term_kind == "cmd":
        # Parse tool info: "[tool] run_terminal #tc_xxx {...}"
        pass

    elif et == "notice":
        seq += 1
        records[str(seq)] = {
            "role": "system",
            "id": _fmt_id(),
            "content": {"text": content},
        }


def _fmt_id():
    return datetime.now().strftime(f"%Y-%-m-%-d-{int(datetime.now().timestamp() * 1000) % 1000000}")

# Write
with open(DST, "w") as f:
    json.dump(records, f, ensure_ascii=False, indent=2)

print(f"Wrote {len(records)} records to {DST}")
