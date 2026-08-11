"""One-off probe: settle how duplicate requestId rows carry usage.

Claude Code writes multiple assistant rows for a single API call (e.g. a text
block and a tool_use block land as separate entries). Each row carries a
`usage` object. The question this script answers is whether those rows

  (a) REPEAT an identical usage object  -> take one per requestId, or
  (b) SPLIT usage across rows           -> sum them.

Getting this wrong is a ~2x error in every figure the tool reports.

Usage:  python3 scripts/probe_dupes.py [~/.claude/projects]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

# Input-side fields are written identically on every row of a request.
# output_tokens is NOT: mid-stream rows carry a partial count.
INPUT_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
USAGE_FIELDS = INPUT_FIELDS + ("output_tokens",)


def usage_signature(usage: dict) -> tuple:
    """The billing-relevant fields only, ignoring nested/derived detail."""
    return tuple(usage.get(f, 0) for f in USAGE_FIELDS)


def input_signature(usage: dict) -> tuple:
    return tuple(usage.get(f, 0) for f in INPUT_FIELDS)


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "~/.claude/projects").expanduser()

    # group[(file, requestId)] = list of (row_index, content_block_types, usage)
    groups: dict[tuple[str, str], list] = defaultdict(list)

    # Recursive: subagent sessions live at
    #   <project>/<parent-session-uuid>/subagents/agent-*.jsonl
    # A shallow */*.jsonl glob silently drops them.
    for path in sorted(root.glob("**/*.jsonl")):
        for i, line in enumerate(path.open(errors="ignore")):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "assistant":
                continue
            msg = row.get("message") or {}
            usage = msg.get("usage")
            if not usage or msg.get("model") == "<synthetic>":
                continue
            key = (str(path), row.get("requestId") or row.get("uuid"))
            groups[key].append((i, msg, usage))

    dupes = {k: v for k, v in groups.items() if len(v) > 1}

    input_identical = sum(1 for r in dupes.values() if len({input_signature(u) for _, _, u in r}) == 1)
    output_differs = [k for k, r in dupes.items() if len({u.get("output_tokens", 0) for _, _, u in r}) > 1]
    no_terminal = [k for k, r in groups.items() if not any(m.get("stop_reason") for _, m, _ in r)]

    print(f"total requestId groups        : {len(groups)}")
    print(f"groups with >1 row            : {len(dupes)}")
    print(f"  INPUT-side identical        : {input_identical}/{len(dupes)}")
    print(f"  output_tokens differs       : {len(output_differs)}")
    print(f"groups with no stop_reason row: {len(no_terminal)}")
    print()
    print("VERDICT: input fields repeat -> take any; output_tokens is cumulative")
    print("         mid-stream -> take the TERMINAL row (stop_reason set).")
    print()

    for key in output_differs[:3]:
        print(f"--- output differs: {Path(key[0]).name} {key[1]}")
        for idx, msg, usage in groups[key]:
            print(
                f"    line {idx:5d} stop={str(msg.get('stop_reason')):9s} "
                f"out={usage.get('output_tokens', 0):>7,}"
            )

    def terminal(rows):
        for _, m, u in rows:
            if m.get("stop_reason"):
                return u
        return max((u for _, _, u in rows), key=lambda u: u.get("output_tokens", 0))

    correct = sum(terminal(r).get("output_tokens", 0) for r in groups.values())
    first_row = sum(r[0][2].get("output_tokens", 0) for r in groups.values())
    summed = sum(u.get("output_tokens", 0) for r in groups.values() for _, _, u in r)

    print(f"\noutput_tokens, TERMINAL row (correct) : {correct:,}")
    print(f"output_tokens, first row              : {first_row:,}  ({correct - first_row:+,})")
    print(f"output_tokens, summed                 : {summed:,}  ({summed / correct:.2f}x inflation)")


if __name__ == "__main__":
    main()
