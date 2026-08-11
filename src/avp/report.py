"""Terminal report and JSON artifact."""

from __future__ import annotations

import json
from pathlib import Path

from .parser import ParseStats
from .pricing import Bucket, Priced, bucket_by, bucket_by_month
from .rates import RateBook


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _fmt_money(x: float) -> str:
    if x and abs(x) < 0.01:
        return "<$0.01"
    return f"${x:,.2f}"


def _rule(width: int = 88) -> str:
    return "-" * width


def _table(title: str, buckets: list[Bucket]) -> str:
    lines = [f"\n{title}", _rule()]
    lines.append(
        f"{'':<34}{'calls':>7}{'in':>9}{'out':>9}{'cache w':>9}{'cache r':>9}{'cost':>11}"
    )
    for b in buckets:
        lines.append(
            f"{b.label[:33]:<34}{b.requests:>7,}"
            f"{_fmt_tokens(b.input_tokens):>9}"
            f"{_fmt_tokens(b.output_tokens):>9}"
            f"{_fmt_tokens(b.cache_write_tokens):>9}"
            f"{_fmt_tokens(b.cache_read_tokens):>9}"
            f"{_fmt_money(b.cost.total):>11}"
        )
    total = sum(b.cost.total for b in buckets)
    lines.append(_rule())
    lines.append(
        f"{'TOTAL':<34}{sum(b.requests for b in buckets):>7,}"
        f"{_fmt_tokens(sum(b.input_tokens for b in buckets)):>9}"
        f"{_fmt_tokens(sum(b.output_tokens for b in buckets)):>9}"
        f"{_fmt_tokens(sum(b.cache_write_tokens for b in buckets)):>9}"
        f"{_fmt_tokens(sum(b.cache_read_tokens for b in buckets)):>9}"
        f"{_fmt_money(total):>11}"
    )
    return "\n".join(lines)


def render(
    priced: list[Priced],
    book: RateBook,
    stats: ParseStats,
    plan_key: str,
    plans: dict,
    group_by: str = "project",
) -> str:
    out: list[str] = []

    # --- rates provenance -------------------------------------------------
    latest = book.latest
    out.append("RATES")
    out.append(_rule())
    out.append(f"  snapshots available : {len(book.snapshots)} ({book.snapshots[0].date} .. {latest.date})")
    for src in latest.sources:
        out.append(f"  source              : {src}")
    out.append(f"  fetched             : {latest.fetched_at or 'unknown'}")
    for note in latest.disagreements:
        out.append(f"  ! {note}")

    from datetime import date as _date

    if latest.date != _date.today().isoformat():
        out.append(
            f"  ! STALE: newest snapshot is {latest.date}, not today. "
            "Prices may have moved; re-run with network access."
        )

    # --- spend tables -----------------------------------------------------
    out.append(_table("BY MODEL", bucket_by(priced, lambda r: r.model)))

    if group_by == "session":
        out.append(_table("BY SESSION", bucket_by(priced, lambda r: r.session_id)))
    else:
        out.append(_table("BY PROJECT", bucket_by(priced, lambda r: r.project)))

    sidechain = [p for p in priced if p.record.is_sidechain]
    if sidechain:
        sc_cost = sum(p.cost.total for p in sidechain)
        total = sum(p.cost.total for p in priced)
        share = sc_cost / total * 100 if total else 0
        out.append(
            f"\n  of which subagent traffic: {len(sidechain):,} calls, "
            f"{_fmt_money(sc_cost)} ({share:.1f}%)"
        )

    # --- monthly vs plan --------------------------------------------------
    plan = plans["plans"].get(plan_key)
    months = bucket_by_month(priced)

    out.append("\nMONTHLY vs PLAN")
    out.append(_rule())
    if plan is None:
        out.append(f"  unknown plan {plan_key!r}; known: {', '.join(plans['plans'])}")
    else:
        price = plan["monthly"]
        out.append(f"  plan: {plan['label']} at {_fmt_money(price)}/month (from data/plans.json)")
        out.append("")
        out.append(f"  {'month':<10}{'calls':>8}{'API equivalent':>18}{'vs plan':>26}")
        for m in months:
            api = m.cost.total
            if price == 0:
                verdict = "free plan"
            elif api > price:
                verdict = f"API costs {api / price:.1f}x more"
            elif api > 0:
                verdict = f"plan costs {price / api:.1f}x more"
            else:
                verdict = "no usage"
            out.append(f"  {m.label:<10}{m.requests:>8,}{_fmt_money(api):>18}{verdict:>26}")

        total_api = sum(m.cost.total for m in months)
        total_plan = price * len(months)
        out.append(_rule())
        out.append(
            f"  {'ALL':<10}{sum(m.requests for m in months):>8,}"
            f"{_fmt_money(total_api):>18}"
            f"{_fmt_money(total_plan) + ' paid':>26}"
        )
        if total_api and total_plan:
            if total_api > total_plan:
                out.append(
                    f"\n  VERDICT: the plan saved you {_fmt_money(total_api - total_plan)} "
                    f"({total_api / total_plan:.1f}x cheaper than API)."
                )
            else:
                out.append(
                    f"\n  VERDICT: API would have cost {_fmt_money(total_plan - total_api)} less "
                    f"({total_plan / total_api:.1f}x cheaper than the plan)."
                )

    # --- caching ----------------------------------------------------------
    actual = sum(p.cost.total for p in priced)
    uncached = sum(p.uncached_cost for p in priced)
    out.append("\nPROMPT CACHING")
    out.append(_rule())
    out.append(f"  actual (cached)        : {_fmt_money(actual)}")
    out.append(f"  same traffic uncached  : {_fmt_money(uncached)}")
    if uncached > actual:
        out.append(
            f"  saved by caching       : {_fmt_money(uncached - actual)} "
            f"({(1 - actual / uncached) * 100:.0f}% off)"
        )

    # --- coverage ---------------------------------------------------------
    out.append("\nCOVERAGE")
    out.append(_rule())
    out.append(f"  session files parsed   : {stats.files}")
    out.append(f"  billable API calls     : {len(priced):,}")
    out.append(f"  duplicate rows merged  : {stats.collapsed:,} (streamed content blocks)")
    if priced:
        span = (
            f"{min(p.record.timestamp for p in priced).date()} .. "
            f"{max(p.record.timestamp for p in priced).date()}"
        )
        out.append(f"  date range             : {span}")
    if stats.malformed:
        out.append(f"  ! malformed lines      : {stats.malformed} (skipped)")
    if stats.synthetic:
        out.append(f"  <synthetic> rows       : {stats.synthetic} (never billed, excluded)")
    if stats.groups_without_terminal:
        out.append(
            f"  ! calls with no stop_reason: {stats.groups_without_terminal} "
            "(used max output_tokens)"
        )
    if stats.inconsistent_input:
        out.append(
            f"  !! calls with inconsistent input tokens: {stats.inconsistent_input} "
            "-- collapse assumption may not hold, investigate"
        )
    if book.fast_mode_hits:
        out.append(
            f"  ! fast-mode calls      : {book.fast_mode_hits} priced at 2x "
            "(derived rule; not in any rate feed)"
        )
    ws = sum(p.record.web_search_requests for p in priced)
    if ws:
        out.append(f"  ! web searches         : {ws} priced at $10/1k (derived rule)")
    if book.used_fallback_snapshot:
        out.append(
            "  ! some calls predate the earliest rate snapshot and were priced "
            "with it; their true rate may have differed."
        )

    return "\n".join(out)


def to_json(priced: list[Priced], book: RateBook, stats: ParseStats, plan_key: str, plans: dict) -> dict:
    plan = plans["plans"].get(plan_key) or {}
    return {
        "generated_from": "claude-code",
        "plan": {"key": plan_key, **plan},
        "rates": {
            "snapshot": book.latest.date,
            "sources": book.latest.sources,
            "disagreements": book.latest.disagreements,
            "snapshots_available": [s.date for s in book.snapshots],
        },
        "totals": {
            "calls": len(priced),
            "cost": round(sum(p.cost.total for p in priced), 6),
            "cost_uncached": round(sum(p.uncached_cost for p in priced), 6),
        },
        "by_model": [
            {
                "label": b.label,
                "requests": b.requests,
                "input_tokens": b.input_tokens,
                "output_tokens": b.output_tokens,
                "cache_write_tokens": b.cache_write_tokens,
                "cache_read_tokens": b.cache_read_tokens,
                "cost": round(b.cost.total, 6),
            }
            for b in bucket_by(priced, lambda r: r.model)
        ],
        "by_project": [
            {"label": b.label, "requests": b.requests, "cost": round(b.cost.total, 6)}
            for b in bucket_by(priced, lambda r: r.project)
        ],
        "by_month": [
            {
                "month": b.label,
                "requests": b.requests,
                "cost": round(b.cost.total, 6),
                "plan_cost": plan.get("monthly"),
            }
            for b in bucket_by_month(priced)
        ],
        "coverage": {
            "files": stats.files,
            "malformed_lines": stats.malformed,
            "synthetic_rows": stats.synthetic,
            "duplicate_rows_merged": stats.collapsed,
            "calls_without_stop_reason": stats.groups_without_terminal,
            "calls_with_inconsistent_input": stats.inconsistent_input,
            "fast_mode_calls": book.fast_mode_hits,
        },
    }


def write_json(payload: dict, path: Path) -> None:
    Path(path).write_text(json.dumps(payload, indent=2))
