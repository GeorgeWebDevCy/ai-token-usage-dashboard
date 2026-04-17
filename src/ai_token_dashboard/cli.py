"""Terminal subcommands: quick summaries without running the daemon.

Inspired by phuryn/claude-usage which exposes ``today`` / ``stats`` /
``scan``. We keep the daemon as the default (no subcommand) so single-command
launches still Just Work, but these are handy when you don't want to run the
server at all.

Commands:
  today   — print today's totals by source/model
  stats   — print totals over a window (default 7 days)
  scan    — one-shot: walk configured JSONL paths and ingest any not-yet-seen
            events, then exit. Uses the existing ``ingest_existing=True`` flag
            on JsonlTailCollector.
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

from .config import AppConfig, DEFAULT_APP_DIR
from .db import Database
from .fx import FxService, format_money


def _fmt(n) -> str:
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def _print_table(rows: list[dict], cols: list[tuple[str, str]], title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    if not rows:
        print("  (no data)")
        return
    # Compute widths.
    widths = [max(len(label), max((len(str(r.get(key, ""))) for r in rows), default=0))
              for key, label in cols]
    header = "  ".join(f"{label:<{w}}" for (key, label), w in zip(cols, widths))
    print(header)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(f"{str(r.get(key, '')):<{w}}" for (key, label), w in zip(cols, widths)))


def _fx_for(cfg: AppConfig) -> FxService:
    return FxService(
        cache_path=DEFAULT_APP_DIR / "fx_cache.json",
        fallback_rate=cfg.display.usd_rate_fallback,
        override_rate=cfg.display.usd_rate_override,
    )


def cmd_today(cfg: AppConfig) -> int:
    db = Database(cfg.db_path)
    fx = _fx_for(cfg)
    currency = cfg.display.currency
    start = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    totals = db.totals_since(start)
    cost = fx.convert(float(totals["cost_usd"] or 0), currency)
    print(f"Today ({dt.date.today().isoformat()}):")
    print(f"  Input tokens:        {_fmt(totals['input_tokens'])}")
    print(f"  Output tokens:       {_fmt(totals['output_tokens'])}")
    print(f"  Cache read tokens:   {_fmt(totals['cache_read_tokens'])}")
    print(f"  Cache write tokens:  {_fmt(totals['cache_write_tokens'])}")
    print(f"  Assistant turns:     {_fmt(totals['event_count'])}")
    print(f"  Estimated cost:      {format_money(cost, currency)}  "
          f"(USD ${totals['cost_usd']:.2f})")
    rows = db.by_model_since(start)
    for r in rows:
        r["cost"] = format_money(fx.convert(float(r["cost_usd"] or 0), currency), currency, decimals=4)
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            r[k] = _fmt(r[k])
    _print_table(
        rows,
        [("source", "Source"), ("model", "Model"),
         ("input_tokens", "Input"), ("output_tokens", "Output"),
         ("cache_read_tokens", "Cache R"), ("cache_write_tokens", "Cache W"),
         ("cost", f"Cost ({currency})"), ("event_count", "Turns")],
        "By model today",
    )
    return 0


def cmd_stats(cfg: AppConfig, days: float = 7.0) -> int:
    db = Database(cfg.db_path)
    fx = _fx_for(cfg)
    currency = cfg.display.currency
    since = time.time() - days * 86400
    totals = db.totals_since(since)
    cost = fx.convert(float(totals["cost_usd"] or 0), currency)
    print(f"Last {days:g} days:")
    print(f"  Input tokens:        {_fmt(totals['input_tokens'])}")
    print(f"  Output tokens:       {_fmt(totals['output_tokens'])}")
    print(f"  Cache read tokens:   {_fmt(totals['cache_read_tokens'])}")
    print(f"  Cache write tokens:  {_fmt(totals['cache_write_tokens'])}")
    print(f"  Assistant turns:     {_fmt(totals['event_count'])}")
    print(f"  Estimated cost:      {format_money(cost, currency)}  "
          f"(USD ${totals['cost_usd']:.2f})")

    by_src = db.by_source_since(since)
    for r in by_src:
        r["tokens"] = _fmt(r["tokens"])
        r["cost"] = format_money(fx.convert(float(r["cost_usd"] or 0), currency), currency)
    _print_table(
        by_src,
        [("source", "Source"), ("tokens", "Tokens"),
         ("cost", f"Cost ({currency})"), ("event_count", "Turns")],
        "By source",
    )

    from .advisor import generate
    tips = generate(cfg.db_path, days=days)
    if tips:
        print("\nTop suggestions:")
        for s in tips[:3]:
            if s.estimated_monthly_savings_usd > 0:
                saved_local = fx.convert(s.estimated_monthly_savings_usd, currency)
                saving = f"  (~{format_money(saved_local, currency)}/mo)"
            else:
                saving = ""
            print(f"  [{s.severity}] {s.title}{saving}")
            print(f"      {s.action}")
    return 0


def cmd_scan(cfg: AppConfig) -> int:
    """Walk configured JSONL paths and ingest every event they contain.

    Unlike the daemon (which seeds file offsets to 'now' and only tracks new
    activity), ``scan`` reads the files end-to-end. Inserts are idempotent via
    the per-event id, so rerunning is safe.
    """
    import asyncio
    from .collectors.claude_code import ClaudeCodeCollector
    from .collectors.codex import CodexCollector
    from .collectors.gemini import GeminiCollector

    db = Database(cfg.db_path)
    loop = asyncio.new_event_loop()
    mapping = {
        "claude_code": ClaudeCodeCollector,
        "codex":       CodexCollector,
        "gemini":      GeminiCollector,
    }
    before = db.totals_since(0)["event_count"]
    for name, cls in mapping.items():
        c = cfg.collectors.get(name)
        if not c or not c.enabled:
            continue
        paths = [p for p in c.resolved_paths if p.exists()]
        if not paths:
            print(f"[{name}] no paths found, skipping")
            continue
        print(f"[{name}] scanning {len(paths)} path(s)…")
        coll = cls(paths=paths, db=db, loop=loop, ingest_existing=True)
        # Walk files directly; we don't need the watchdog observer for a one-shot.
        for root in paths:
            for jsonl in root.rglob("*.jsonl"):
                coll._drain(jsonl)  # noqa: SLF001 — intentional one-shot
    after = db.totals_since(0)["event_count"]
    print(f"\nScan complete. Inserted {after - before} new events "
          f"({after} total in DB).")
    loop.close()
    return 0


def dispatch(argv: list[str]) -> int:
    """Return exit code, or -1 if no subcommand was given and caller should run daemon."""
    if not argv or argv[0].startswith("-"):
        return -1

    import argparse
    parser = argparse.ArgumentParser(prog="ai-token-dashboard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_today = sub.add_parser("today", help="Print today's token totals")
    p_today.add_argument("--config", type=Path, default=None)

    p_stats = sub.add_parser("stats", help="Print totals over the last N days")
    p_stats.add_argument("--days", type=float, default=7.0)
    p_stats.add_argument("--config", type=Path, default=None)

    p_scan = sub.add_parser("scan", help="Backfill: ingest all existing JSONL history")
    p_scan.add_argument("--config", type=Path, default=None)

    # Let --help work even if the first arg is a subcommand name.
    if argv[0] not in {"today", "stats", "scan"}:
        return -1

    args = parser.parse_args(argv)
    cfg = AppConfig.load(args.config)
    if args.cmd == "today":
        return cmd_today(cfg)
    if args.cmd == "stats":
        return cmd_stats(cfg, days=args.days)
    if args.cmd == "scan":
        return cmd_scan(cfg)
    return 0
