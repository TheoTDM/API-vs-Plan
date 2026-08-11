"""Live model pricing.

Rates are fetched on every run and snapshotted to a dated cache. Nothing is
hand-maintained.

WHY SNAPSHOTS: a live feed only ever knows *today's* prices. Anthropic runs
time-boxed promotions (Sonnet 5 shipped at an introductory $2/$10 through
2026-08-31, against a $3/$15 list price). Pricing every historical call at
today's rate would silently rewrite past months the moment a promo lapses. So
each fetch is written to ~/.cache/avp/rates/<date>.json, and a call is priced
against the snapshot nearest its own timestamp. The longer the tool is used,
the more accurate its history becomes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from .models import RateSnapshot, Rates

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
MODELS_DEV_URL = "https://models.dev/api.json"

CACHE_DIR = Path("~/.cache/avp/rates").expanduser()
TIMEOUT = 30

# Not published in any machine-readable feed. Kept here, named and commented,
# so they are visible rather than buried in an expression. The report flags
# every record priced with one of these.
WEB_SEARCH_PER_1K = 10.0
FAST_MODE_MULTIPLIER = 2.0
FAST_MODE_MODELS = {"claude-opus-5", "claude-opus-4-8"}


class RateError(RuntimeError):
    """Rates could not be established. Never fall back to invented numbers."""


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "api-vs-plan"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _from_litellm(doc: dict) -> dict[str, Rates]:
    """First-party Anthropic entries only.

    The file also carries Bedrock and Vertex variants of the same models at
    different prices -- `anthropic.claude-sonnet-5`, `au.anthropic.claude-...`
    (a region premium). Those must never match a bare id from the logs, so
    filter on the provider field rather than on the key string.
    """
    out: dict[str, Rates] = {}
    for key, val in doc.items():
        if not isinstance(val, dict) or val.get("litellm_provider") != "anthropic":
            continue
        if val.get("input_cost_per_token") is None:
            continue
        inp = float(val["input_cost_per_token"])
        out[key] = Rates(
            input=inp,
            output=float(val.get("output_cost_per_token") or 0.0),
            # Fall back to the documented multipliers only if the feed omits a
            # field, so a schema change degrades predictably instead of zeroing.
            cache_write_5m=float(val.get("cache_creation_input_token_cost") or inp * 1.25),
            cache_write_1h=float(
                val.get("cache_creation_input_token_cost_above_1hr") or inp * 2.0
            ),
            cache_read=float(val.get("cache_read_input_token_cost") or inp * 0.1),
        )
    return out


def _from_models_dev(doc: dict) -> dict[str, Rates]:
    """Cross-check source. Costs are per million tokens here."""
    out: dict[str, Rates] = {}
    models = (doc.get("anthropic") or {}).get("models") or {}
    for key, val in models.items():
        cost = (val or {}).get("cost") or {}
        if cost.get("input") is None:
            continue
        inp = float(cost["input"]) / 1e6
        out[key] = Rates(
            input=inp,
            output=float(cost.get("output") or 0.0) / 1e6,
            cache_write_5m=float(cost.get("cache_write") or 0.0) / 1e6 or inp * 1.25,
            cache_write_1h=inp * 2.0,  # models.dev has no 1h tier
            cache_read=float(cost.get("cache_read") or 0.0) / 1e6 or inp * 0.1,
        )
    return out


def _cross_check(primary: dict[str, Rates], other: dict[str, Rates]) -> list[str]:
    """Flag models where the two feeds disagree by more than 1%."""
    notes: list[str] = []
    for model, a in primary.items():
        b = other.get(model)
        if b is None:
            continue
        for field in ("input", "output"):
            x, y = getattr(a, field), getattr(b, field)
            if x and abs(x - y) / x > 0.01:
                notes.append(
                    f"{model}.{field}: litellm={x * 1e6:.2f} models.dev={y * 1e6:.2f} per MTok"
                )
    return notes


def fetch() -> RateSnapshot:
    """Fetch and normalise rates from both feeds."""
    try:
        primary = _from_litellm(_get_json(LITELLM_URL))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RateError(f"could not fetch rates from {LITELLM_URL}: {exc}") from exc
    if not primary:
        raise RateError("rate feed returned no first-party Anthropic models")

    sources = [LITELLM_URL]
    disagreements: list[str] = []
    try:
        secondary = _from_models_dev(_get_json(MODELS_DEV_URL))
        sources.append(MODELS_DEV_URL)
        disagreements = _cross_check(primary, secondary)
    except (urllib.error.URLError, OSError, ValueError):
        disagreements = ["cross-check source unavailable; rates unverified"]

    today = date.today().isoformat()
    return RateSnapshot(
        date=today,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        sources=sources,
        rates=primary,
        disagreements=disagreements,
    )


def _snapshot_path(day: str) -> Path:
    return CACHE_DIR / f"{day}.json"


def save(snapshot: RateSnapshot) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(snapshot.date)
    path.write_text(
        json.dumps(
            {
                "date": snapshot.date,
                "fetched_at": snapshot.fetched_at,
                "sources": snapshot.sources,
                "disagreements": snapshot.disagreements,
                "rates": {m: vars(r) for m, r in snapshot.rates.items()},
            },
            indent=2,
            sort_keys=True,
        )
    )
    return path


def _load(path: Path) -> RateSnapshot:
    doc = json.loads(path.read_text())
    return RateSnapshot(
        date=doc["date"],
        fetched_at=doc.get("fetched_at", ""),
        sources=doc.get("sources", []),
        rates={m: Rates(**v) for m, v in doc["rates"].items()},
        disagreements=doc.get("disagreements", []),
    )


def load_all() -> list[RateSnapshot]:
    """Every cached snapshot, oldest first."""
    if not CACHE_DIR.exists():
        return []
    snaps = []
    for path in sorted(CACHE_DIR.glob("*.json")):
        try:
            snaps.append(_load(path))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue  # a corrupt snapshot must not break the run
    return sorted(snaps, key=lambda s: s.date)


def ensure(refresh: bool = False, offline: bool = False) -> list[RateSnapshot]:
    """Return all snapshots, fetching today's first unless told not to.

    Failure is explicit: with no network and no cache, this raises rather than
    guessing at prices.
    """
    snaps = load_all()
    have_today = any(s.date == date.today().isoformat() for s in snaps)

    if offline:
        if not snaps:
            raise RateError(
                "--offline requires a cached rate snapshot, none found in "
                f"{CACHE_DIR}. Run once with network access first."
            )
        return snaps

    if refresh or not have_today:
        try:
            snap = fetch()
            save(snap)
            snaps = [s for s in snaps if s.date != snap.date] + [snap]
            snaps.sort(key=lambda s: s.date)
        except RateError:
            if not snaps:
                raise
            # Network down but cache present: proceed, and let the report warn.
    return snaps


class RateBook:
    """Resolves (model, timestamp) -> Rates against dated snapshots."""

    def __init__(self, snapshots: list[RateSnapshot]) -> None:
        if not snapshots:
            raise RateError("no rate snapshots available")
        self.snapshots = sorted(snapshots, key=lambda s: s.date)
        self.used_fallback_snapshot = False
        self.fast_mode_hits = 0
        self.unresolved: set[str] = set()

    @property
    def latest(self) -> RateSnapshot:
        return self.snapshots[-1]

    def _snapshot_for(self, when: datetime) -> RateSnapshot:
        day = when.date().isoformat()
        candidates = [s for s in self.snapshots if s.date <= day]
        if not candidates:
            # Call predates every snapshot: use the earliest known rates and
            # say so, rather than pretending the price is authoritative.
            self.used_fallback_snapshot = True
            return self.snapshots[0]
        return candidates[-1]

    def _lookup(self, snapshot: RateSnapshot, model: str) -> Rates | None:
        if model in snapshot.rates:
            return snapshot.rates[model]
        # Trim a trailing date suffix: claude-haiku-4-5-20251001 -> claude-haiku-4-5
        parts = model.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
            return snapshot.rates.get(parts[0])
        return None

    def rates_for(self, model: str, when: datetime, speed: str = "standard") -> Rates:
        snapshot = self._snapshot_for(when)
        base = self._lookup(snapshot, model)
        if base is None:
            self.unresolved.add(model)
            raise RateError(
                f"no published rate for model {model!r} in the {snapshot.date} "
                "snapshot. Run with --refresh; if it persists the feed has not "
                "listed this model yet."
            )
        if speed == "fast" and model in FAST_MODE_MODELS:
            self.fast_mode_hits += 1
            return Rates(
                input=base.input * FAST_MODE_MULTIPLIER,
                output=base.output * FAST_MODE_MULTIPLIER,
                cache_write_5m=base.cache_write_5m * FAST_MODE_MULTIPLIER,
                cache_write_1h=base.cache_write_1h * FAST_MODE_MULTIPLIER,
                cache_read=base.cache_read * FAST_MODE_MULTIPLIER,
            )
        return base
