"""Tests for parsing and pricing.

Run:  PYTHONPATH=src python3 -m unittest discover -s tests -v

The fixture under tests/fixtures/ is entirely synthetic -- it was authored by
hand to exercise specific log shapes, not redacted from real sessions, so it
contains no prompt content, source code, or filesystem paths.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from avp.models import RateSnapshot, Rates, UsageRecord
from avp.parser import ParseStats, parse
from avp.pricing import price_all, price_record
from avp.rates import RateBook, RateError

FIXTURES = Path(__file__).parent / "fixtures"
LOGS = FIXTURES / "projects"


def load_book() -> RateBook:
    doc = json.loads((FIXTURES / "rates-2026-07-01.json").read_text())
    snap = RateSnapshot(
        date=doc["date"],
        fetched_at=doc["fetched_at"],
        sources=doc["sources"],
        rates={m: Rates(**v) for m, v in doc["rates"].items()},
        disagreements=doc["disagreements"],
    )
    return RateBook([snap])


class TestParser(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = ParseStats()
        self.records = parse(LOGS, self.stats)
        self.by_id = {r.request_id: r for r in self.records}

    def test_one_record_per_request(self) -> None:
        # 3 billable requests in the main log (req_aaa, req_bbb, req_ddd) plus
        # 1 subagent request. req_ccc is <synthetic> and must be excluded.
        self.assertEqual(len(self.records), 4)
        self.assertEqual(self.stats.synthetic, 1)
        self.assertEqual(
            sorted(r.request_id for r in self.records),
            ["req_aaa", "req_bbb", "req_ddd", "req_sub"],
        )

    def test_duplicate_rows_are_collapsed_not_summed(self) -> None:
        rec = self.by_id["req_aaa"]
        self.assertEqual(rec.output_tokens, 50)  # not 100
        self.assertEqual(rec.input_tokens, 100)  # not 200

    def test_terminal_row_wins_over_partial_stream(self) -> None:
        # Mid-stream row said 8; the stop_reason row said 9000.
        rec = self.by_id["req_bbb"]
        self.assertEqual(rec.output_tokens, 9000)

    def test_cache_ttl_split_is_preserved(self) -> None:
        self.assertEqual(self.by_id["req_aaa"].cache_creation_5m, 200)
        self.assertEqual(self.by_id["req_aaa"].cache_creation_1h, 0)
        self.assertEqual(self.by_id["req_bbb"].cache_creation_5m, 0)
        self.assertEqual(self.by_id["req_bbb"].cache_creation_1h, 300)

    def test_subagent_found_and_attributed_to_parent_project(self) -> None:
        rec = self.by_id["req_sub"]
        self.assertTrue(rec.is_sidechain)
        # Rolls up to the top-level project, not a phantom "subagents" project.
        self.assertEqual(rec.project, "proj-alpha")

    def test_malformed_line_is_counted_not_fatal(self) -> None:
        self.assertEqual(self.stats.malformed, 1)

    def test_input_fields_consistent_within_request(self) -> None:
        self.assertEqual(self.stats.inconsistent_input, 0)


class TestPricing(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_book()
        self.when = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def _rec(self, **kw) -> UsageRecord:
        base = dict(
            source="claude-code",
            project="p",
            session_id="s",
            request_id="r",
            timestamp=self.when,
            model="claude-opus-5",
            input_tokens=0,
            output_tokens=0,
            cache_creation_5m=0,
            cache_creation_1h=0,
            cache_read=0,
        )
        base.update(kw)
        return UsageRecord(**base)

    def test_basic_cost(self) -> None:
        # 1M in @ $5, 1M out @ $25
        rec = self._rec(input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(price_record(rec, self.book).cost.total, 30.0, places=6)

    def test_cache_tiers_use_feed_rates(self) -> None:
        # 1M 5m-write @ $6.25, 1M 1h-write @ $10, 1M read @ $0.50
        rec = self._rec(
            cache_creation_5m=1_000_000, cache_creation_1h=1_000_000, cache_read=1_000_000
        )
        self.assertAlmostEqual(price_record(rec, self.book).cost.total, 16.75, places=6)

    def test_fast_mode_is_exactly_double(self) -> None:
        kw = dict(input_tokens=500_000, output_tokens=100_000)
        std = price_record(self._rec(speed="standard", **kw), self.book).cost.total
        fast = price_record(self._rec(speed="fast", **kw), self.book).cost.total
        self.assertAlmostEqual(fast, std * 2, places=9)

    def test_fast_mode_ignored_for_models_without_it(self) -> None:
        kw = dict(model="claude-sonnet-5", input_tokens=500_000)
        std = price_record(self._rec(speed="standard", **kw), self.book).cost.total
        fast = price_record(self._rec(speed="fast", **kw), self.book).cost.total
        self.assertAlmostEqual(fast, std, places=9)

    def test_web_search_priced_per_thousand(self) -> None:
        rec = self._rec(web_search_requests=1000)
        self.assertAlmostEqual(price_record(rec, self.book).cost.tools, 10.0, places=6)

    def test_unknown_model_raises_rather_than_costing_zero(self) -> None:
        rec = self._rec(model="claude-not-a-real-model")
        with self.assertRaises(RateError):
            price_record(rec, self.book)

    def test_dated_model_id_falls_back_to_base(self) -> None:
        rec = self._rec(model="claude-haiku-4-5-20251001", input_tokens=1_000_000)
        self.assertAlmostEqual(price_record(rec, self.book).cost.total, 1.0, places=6)

    def test_bedrock_style_id_is_not_matched(self) -> None:
        rec = self._rec(model="anthropic.claude-opus-5")
        with self.assertRaises(RateError):
            price_record(rec, self.book)

    def test_uncached_counterfactual_is_higher(self) -> None:
        rec = self._rec(cache_read=1_000_000)
        p = price_record(rec, self.book)
        self.assertAlmostEqual(p.cost.total, 0.5, places=6)   # read rate
        self.assertAlmostEqual(p.uncached_cost, 5.0, places=6)  # full input rate

    def test_predating_snapshot_is_flagged(self) -> None:
        rec = self._rec(timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc), input_tokens=1)
        price_record(rec, self.book)
        self.assertTrue(self.book.used_fallback_snapshot)


class TestEndToEnd(unittest.TestCase):
    def test_fixture_prices_without_error(self) -> None:
        book = load_book()
        records = parse(LOGS, ParseStats())
        priced = price_all(records, book)
        self.assertEqual(len(priced), 4)
        self.assertGreater(sum(p.cost.total for p in priced), 0)


if __name__ == "__main__":
    unittest.main()
