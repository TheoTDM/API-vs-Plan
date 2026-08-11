"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import report
from .parser import ParseStats, parse
from .pricing import price_all
from .rates import RateBook, RateError, ensure

DEFAULT_LOGS = "~/.claude/projects"
PLANS_FILE = Path(__file__).resolve().parents[2] / "data" / "plans.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="avp",
        description="Compare what your Claude plan costs against the API-equivalent "
        "price of the same Claude Code usage.",
    )
    p.add_argument("--dir", default=DEFAULT_LOGS, help=f"session log root (default {DEFAULT_LOGS})")
    p.add_argument("--plan", default="max-5x", help="plan key from data/plans.json (default max-5x)")
    p.add_argument("--by", choices=("project", "session"), default="project", help="second table grouping")
    p.add_argument("--json", dest="json_out", metavar="PATH", help="also write structured JSON here")
    p.add_argument("--refresh", action="store_true", help="force a rate re-fetch even if today's snapshot exists")
    p.add_argument("--offline", action="store_true", help="never touch the network; require a cached snapshot")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.dir).expanduser()
    if not root.exists():
        print(f"error: no such log directory: {root}", file=sys.stderr)
        return 2

    try:
        snapshots = ensure(refresh=args.refresh, offline=args.offline)
        book = RateBook(snapshots)
    except RateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    stats = ParseStats()
    records = parse(root, stats)
    if not records:
        print(f"No billable Claude Code calls found under {root}.", file=sys.stderr)
        return 1

    try:
        priced = price_all(records, book)
    except RateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    plans = json.loads(PLANS_FILE.read_text())

    print(report.render(priced, book, stats, args.plan, plans, group_by=args.by))

    if args.json_out:
        payload = report.to_json(priced, book, stats, args.plan, plans)
        report.write_json(payload, Path(args.json_out))
        print(f"\nwrote {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
