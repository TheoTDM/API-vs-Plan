"""Cost computation.

Rates are USD per single token exactly as published, so there is no per-million
scaling anywhere in this module -- that removes a whole class of unit bug.

Cache multipliers are NOT hardcoded here: cache_write_5m, cache_write_1h and
cache_read come straight from the feed's own fields (see rates.py), so if
Anthropic changes a tier the tool follows without a code edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import UsageRecord
from .rates import WEB_SEARCH_PER_1K, RateBook


@dataclass
class Cost:
    """Cost of one or more records, broken out by what drove it."""

    input: float = 0.0
    output: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0
    tools: float = 0.0

    @property
    def total(self) -> float:
        return self.input + self.output + self.cache_write + self.cache_read + self.tools

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_write=self.cache_write + other.cache_write,
            cache_read=self.cache_read + other.cache_read,
            tools=self.tools + other.tools,
        )


@dataclass
class Priced:
    """A record with its cost, plus the counterfactual no-cache cost."""

    record: UsageRecord
    cost: Cost
    uncached_cost: float  # same traffic with every input token at full rate


def price_record(record: UsageRecord, book: RateBook) -> Priced:
    r = book.rates_for(record.model, record.timestamp, record.speed)

    cost = Cost(
        input=record.input_tokens * r.input,
        output=record.output_tokens * r.output,
        cache_write=(
            record.cache_creation_5m * r.cache_write_5m
            + record.cache_creation_1h * r.cache_write_1h
        ),
        cache_read=record.cache_read * r.cache_read,
        tools=record.web_search_requests / 1000 * WEB_SEARCH_PER_1K,
    )

    # What this call would have cost with prompt caching switched off: every
    # input-side token billed at the full input rate. Exact arithmetic on
    # recorded counts, not a model of some hypothetical rewrite.
    uncached = (
        record.total_input * r.input
        + record.output_tokens * r.output
        + cost.tools
    )

    return Priced(record=record, cost=cost, uncached_cost=uncached)


def price_all(records: list[UsageRecord], book: RateBook) -> list[Priced]:
    return [price_record(rec, book) for rec in records]


@dataclass
class Bucket:
    """Aggregate over a group of priced records."""

    label: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    web_search_requests: int = 0
    cost: Cost = field(default_factory=Cost)
    uncached_cost: float = 0.0

    def add(self, p: Priced) -> None:
        rec = p.record
        self.requests += 1
        self.input_tokens += rec.input_tokens
        self.output_tokens += rec.output_tokens
        self.cache_write_tokens += rec.cache_creation_5m + rec.cache_creation_1h
        self.cache_read_tokens += rec.cache_read
        self.web_search_requests += rec.web_search_requests
        self.cost = self.cost + p.cost
        self.uncached_cost += p.uncached_cost

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
        )


def bucket_by(priced: list[Priced], key) -> list[Bucket]:
    """Group priced records by `key(record)`, heaviest spend first."""
    buckets: dict[str, Bucket] = {}
    for p in priced:
        label = key(p.record)
        buckets.setdefault(label, Bucket(label)).add(p)
    return sorted(buckets.values(), key=lambda b: b.cost.total, reverse=True)


def bucket_by_month(priced: list[Priced]) -> list[Bucket]:
    """Group by calendar month, chronologically."""
    buckets: dict[str, Bucket] = {}
    for p in priced:
        label = p.record.timestamp.strftime("%Y-%m")
        buckets.setdefault(label, Bucket(label)).add(p)
    return sorted(buckets.values(), key=lambda b: b.label)
