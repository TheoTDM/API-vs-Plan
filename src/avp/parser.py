"""Parse Claude Code session logs into UsageRecords.

Three behaviours here are load-bearing and were established by measuring real
logs (see scripts/probe_dupes.py). Changing any of them silently corrupts every
figure the tool reports:

1. RECURSIVE GLOB. Subagent sessions live at
   <project>/<parent-session-uuid>/subagents/agent-*.jsonl. A shallow
   `*/*.jsonl` misses them entirely -- on the reference logs that dropped 2 of
   11 files of real billed traffic.

2. TERMINAL-ROW COLLAPSE. Claude Code writes several assistant rows per API
   call (one per content block). Input-side fields repeat identically on every
   row, but `output_tokens` is written cumulatively mid-stream: early rows can
   say 8 while the final row says 25,681. Take the row with `stop_reason` set.
   Summing inflates output by ~2.5x; taking the first row undercounts.

3. PATH-BASED SIDECHAIN. The `isSidechain` field is never true in practice.
   Subagent traffic is identified by a `subagents` path component.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .models import UsageRecord

SOURCE = "claude-code"

# Rows whose model is this are locally-generated error text, never billed.
SYNTHETIC_MODEL = "<synthetic>"

# Input-side usage fields, which repeat identically across a request's rows.
_INPUT_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


class ParseStats:
    """Counters surfaced in the report's coverage block."""

    def __init__(self) -> None:
        self.files = 0
        self.lines = 0
        self.malformed = 0
        self.synthetic = 0
        self.rows_with_usage = 0
        self.collapsed = 0  # rows discarded by terminal-row collapse
        self.groups_without_terminal = 0
        self.inconsistent_input = 0


def _project_of(path: Path, root: Path) -> tuple[str, bool]:
    """Return (top-level project dir, is_sidechain).

    Subagent logs nest under their parent session, so the project is always the
    first path component -- that way subagent cost rolls up to the work that
    spawned it rather than appearing as a phantom project.
    """
    rel = path.relative_to(root)
    project = rel.parts[0] if len(rel.parts) > 1 else "(root)"
    return project, "subagents" in rel.parts


def _cache_split(usage: dict) -> tuple[int, int]:
    """(5-minute, 1-hour) cache-creation tokens.

    They bill at different multipliers (1.25x vs 2x), so the split matters.
    Older lines omit the breakdown; treat those as 5m, the cheaper tier, so an
    unknown never silently inflates the bill.
    """
    detail = usage.get("cache_creation") or {}
    five = detail.get("ephemeral_5m_input_tokens")
    hour = detail.get("ephemeral_1h_input_tokens")
    if five is None and hour is None:
        return int(usage.get("cache_creation_input_tokens", 0) or 0), 0
    return int(five or 0), int(hour or 0)


def _terminal(rows: list[dict]) -> dict:
    """The row carrying final cumulative output_tokens.

    Prefer the row with `stop_reason` set. A few groups have no such row (an
    interrupted or still-streaming turn) -- fall back to the largest
    output_tokens, which is the same value the terminal row would have held.
    """
    for msg, _ in rows:
        if msg.get("stop_reason"):
            return msg
    return max((m for m, _ in rows), key=lambda m: (m.get("usage") or {}).get("output_tokens", 0))


def parse(root: Path, stats: ParseStats | None = None) -> list[UsageRecord]:
    """Read every session log under `root` and return one record per API call."""
    root = Path(root).expanduser()
    st = stats if stats is not None else ParseStats()

    # groups[(file, requestId)] -> [(message, row), ...]
    groups: dict[tuple[Path, str], list[tuple[dict, dict]]] = defaultdict(list)

    for path in sorted(root.glob("**/*.jsonl")):
        st.files += 1
        with path.open(errors="ignore") as fh:
            for line in fh:
                st.lines += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # Truncated final line on an in-flight session is normal.
                    st.malformed += 1
                    continue
                if row.get("type") != "assistant":
                    continue
                msg = row.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                if msg.get("model") == SYNTHETIC_MODEL:
                    st.synthetic += 1
                    continue
                st.rows_with_usage += 1
                key = (path, row.get("requestId") or row.get("uuid") or "")
                groups[key].append((msg, row))

    records: list[UsageRecord] = []
    for (path, request_id), rows in groups.items():
        st.collapsed += len(rows) - 1

        # Input-side fields must agree across a group. If they ever don't, the
        # collapse assumption is wrong for that row -- count it loudly rather
        # than silently picking one.
        sigs = {
            tuple((m.get("usage") or {}).get(f, 0) for f in _INPUT_FIELDS) for m, _ in rows
        }
        if len(sigs) > 1:
            st.inconsistent_input += 1

        if not any(m.get("stop_reason") for m, _ in rows):
            st.groups_without_terminal += 1

        msg = _terminal(rows)
        usage = msg.get("usage") or {}
        row = next(r for m, r in rows if m is msg)

        project, sidechain = _project_of(path, root)
        five, hour = _cache_split(usage)
        server_tools = usage.get("server_tool_use") or {}

        ts = row.get("timestamp")
        try:
            timestamp = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            timestamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone()

        records.append(
            UsageRecord(
                source=SOURCE,
                project=project,
                session_id=path.stem,
                request_id=request_id or None,
                timestamp=timestamp,
                model=msg.get("model") or "unknown",
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_creation_5m=five,
                cache_creation_1h=hour,
                cache_read=int(usage.get("cache_read_input_tokens", 0) or 0),
                web_search_requests=int(server_tools.get("web_search_requests", 0) or 0),
                speed=usage.get("speed") or "standard",
                is_sidechain=sidechain,
            )
        )

    records.sort(key=lambda r: r.timestamp)
    return records
