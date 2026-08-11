"""Core data types.

`UsageRecord` is deliberately provider-neutral: it describes one billable model
call, with no Claude-specific fields. Adding a ChatGPT or Cursor parser later
means emitting these, and nothing in rates/pricing/report needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class UsageRecord:
    """One billable API call."""

    source: str  # "claude-code"
    project: str  # top-level project dir; subagent calls roll up to their parent
    session_id: str  # jsonl filename stem
    request_id: str | None
    timestamp: datetime
    model: str

    input_tokens: int
    output_tokens: int
    cache_creation_5m: int
    cache_creation_1h: int
    cache_read: int

    web_search_requests: int = 0
    speed: str = "standard"  # "standard" | "fast"
    is_sidechain: bool = False  # subagent traffic; billed, reported separately

    @property
    def total_input(self) -> int:
        """Every token charged on the input side, at whatever rate."""
        return (
            self.input_tokens
            + self.cache_creation_5m
            + self.cache_creation_1h
            + self.cache_read
        )


@dataclass(frozen=True)
class Rates:
    """USD per single token, exactly as published. No per-million scaling."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float


@dataclass(frozen=True)
class RateSnapshot:
    """Rates as they stood on a given date, plus provenance."""

    date: str  # YYYY-MM-DD
    fetched_at: str  # ISO timestamp
    sources: list[str]
    rates: dict[str, Rates]
    disagreements: list[str]  # cross-source mismatches noted at fetch time
