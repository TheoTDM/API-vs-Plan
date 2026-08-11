# API vs Plan

You pay a flat monthly fee for a Claude subscription. This tool answers a
concrete question: **if the same work had gone through the Anthropic API
instead, what would the bill have been?**

Phase 1 covers **Claude Code sessions**. Those logs record exact token counts
per API call, so every figure below is computed arithmetic, not an estimate.

```
$ avp --plan max-5x

BY MODEL
----------------------------------------------------------------------------------------
                                    calls       in      out  cache w  cache r       cost
claude-sonnet-5                       754       2k     758k     5.9M   309.9M     $93.18
claude-opus-4-8                        48      15k      70k     1.6M     5.8M     $21.00
...

MONTHLY vs PLAN
  month        calls    API equivalent                   vs plan
  2026-07        806           $137.87       API costs 1.4x more
  2026-08         73            $20.63      plan costs 4.8x more

PROMPT CACHING
  actual (cached)        : $163.84
  same traffic uncached  : $946.32
  saved by caching       : $782.47 (83% off)
```

## Quickstart

No dependencies, no virtualenv, no build step.

```bash
git clone https://github.com/TheoTDM/API-vs-Plan.git
cd API-vs-Plan
make install          # symlinks bin/avp into ~/.local/bin
avp --plan max-5x
```

That's it. `avp` now works from any directory.

**Don't want to install anything?** Run it in place — same tool, same flags:

```bash
./bin/avp --plan max-5x
```

`make install` only creates a symlink; `make uninstall` deletes it. Nothing is
written into your Python environment, which matters because Homebrew's Python is
PEP 668 `EXTERNALLY-MANAGED` and would reject `pip install -e .` anyway.

If `avp: command not found`, `~/.local/bin` isn't on your `PATH`:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh
```

`bin/avp` is a small shell launcher that resolves its own symlink, points
`PYTHONPATH` at `src/`, and picks the newest available interpreter
(`python3.13` → `3.12` → `3.11` → `python3`).

## Options

| Flag | Meaning |
|---|---|
| `--dir PATH` | Session log root (default `~/.claude/projects`) |
| `--plan KEY` | Plan to compare against; keys live in `data/plans.json` |
| `--by project\|session` | Grouping for the second table |
| `--json PATH` | Also write a structured JSON artifact |
| `--refresh` | Force a rate re-fetch even if today's snapshot exists |
| `--offline` | Never touch the network; require a cached snapshot |

```bash
avp --plan pro                  # compare against a different plan
avp --by session                # per-session instead of per-project
avp --json out.json             # also write a structured artifact
avp --refresh                   # force fresh rates (normally cached daily)
avp --offline                   # no network; use newest cached snapshot
```

## Pricing is fetched, never hand-maintained

Rates come from
[LiteLLM's `model_prices_and_context_window.json`](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json),
cross-checked against [models.dev](https://models.dev/api.json) where reachable.
There is no pricing field on `GET /v1/models`, and `claude.com/pricing` renders
its rates client-side, so a feed is the only machine-readable option.

Only entries whose provider is `anthropic` are used. The same file also carries
`anthropic.claude-*` (Bedrock) and `au.anthropic.claude-*` (region premium)
variants at different prices; matching one of those against a bare model id from
the logs would silently misprice everything.

**Rates are snapshotted per day** to `~/.cache/avp/rates/<date>.json`, and each
call is priced against the snapshot nearest its own timestamp. A live feed only
knows *today's* price — Anthropic runs time-boxed promotions (Sonnet 5 shipped at
an introductory $2/$10 against a $3/$15 list price), so pricing historical calls
at today's rate would silently rewrite past months when a promo lapses. The
longer the tool is used, the more accurate its history becomes.

If rates cannot be established, the tool **exits non-zero**. It never falls back
to invented numbers.

Two figures are not in any feed and are applied as documented local rules, each
flagged in the report whenever it fires: **fast mode** (Opus 5 / 4.8 at
`speed: "fast"` bill at 2×) and **web search** ($10 per 1,000 requests).

## How the logs are read

Three behaviours are load-bearing. Each was established by measuring real logs
(`scripts/probe_dupes.py`), and each would silently corrupt every figure if
changed:

1. **Recursive glob.** Subagent sessions live at
   `<project>/<parent-session-uuid>/subagents/agent-*.jsonl`. A shallow
   `*/*.jsonl` misses them — on the reference logs it dropped 2 of 11 files of
   real billed traffic. Subagent cost rolls up to the project that spawned it.

2. **Terminal-row collapse.** Claude Code writes one row per content block, so a
   single API call produces several rows that each carry a `usage` object.
   Input-side fields repeat identically, but `output_tokens` is written
   *cumulatively mid-stream*: on real logs an early row read 8 while the final
   row read 25,681. The row with `stop_reason` set is the authoritative one.
   Summing inflates output by ~2.5×; taking the first row undercounts.

3. **Path-based sidechain detection.** The `isSidechain` field is never `true`
   in practice. Subagent traffic is identified by a `subagents` path component.

Rows whose model is `<synthetic>` are locally-generated error text and are never
billed, so they are excluded.

## Caveats

- Claude Code prunes old session logs, so totals shrink as history ages out.
  This reports what is still on disk.
- Calls predating your earliest rate snapshot are priced with that snapshot and
  explicitly flagged.
- Plan prices in `data/plans.json` are hand-set: they are not published in any
  machine-readable feed and `claude.com/pricing` is ambiguous about the Max
  tiers. Edit them to match what you actually pay — the report always prints
  which figure it used.

## Tests

```bash
make test
```

The fixture in `tests/fixtures/` is entirely synthetic, authored to exercise
specific log shapes. It contains no prompt content, source code, or filesystem
paths from real sessions.

## Scope

Claude Code sessions only. claude.ai web and desktop conversations are out of
scope: that export carries no token counts, which would force an estimator and
an uncertainty band. `UsageRecord` is provider-neutral so other tools can be
added later without touching the rates, pricing, or report layers.
