"""Rule-based cost-savings advisor.

Looks at recent events and emits concrete suggestions with estimated savings.
The advisor is intentionally simple (pure SQL + a few Python loops) so each
rule's logic is easy to read, tune, or disable — which matters because these
tips are opinionated and what's "too expensive" depends on the user.

Each rule returns zero or more ``Suggestion`` objects; the engine dedupes by
``rule_id`` + ``context_key`` so you don't see the same tip 50 times.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .pricing import PRICES, cost_for


@dataclass
class Suggestion:
    rule_id: str
    severity: str            # "info" | "tip" | "warn"
    title: str
    body: str
    action: str
    estimated_monthly_savings_usd: float = 0.0
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# --- helpers ---------------------------------------------------------------

def _conn(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def _downgrade_map() -> dict[str, str]:
    """For each premium model, the cheaper sibling we'd suggest for simple turns."""
    return {
        "claude-opus-4-6":   "claude-sonnet-4-6",
        "claude-sonnet-4-6": "claude-haiku-4-5",
        "gpt-5":             "gpt-5-mini",
        "gemini-2.5-pro":    "gemini-2.5-flash",
    }


def _pct(num: float, den: float) -> float:
    return 0.0 if den <= 0 else 100.0 * num / den


# --- individual rules ------------------------------------------------------

def rule_expensive_model_small_output(db_path: Path, since_ts: float) -> Iterable[Suggestion]:
    """Premium models handling turns with very small output tokens.

    Signal: output_tokens < 200 on Opus/GPT-5/Gemini-Pro. The short answer
    probably didn't need the expensive model; suggest the cheaper sibling and
    show how much $ you'd have saved over the window.
    """
    downgrades = _downgrade_map()
    with _conn(db_path) as c:
        rows = c.execute(
            """
            SELECT model,
                   COUNT(*) AS turns,
                   SUM(input_tokens)  AS in_t,
                   SUM(output_tokens) AS out_t,
                   SUM(cost_usd)      AS cost
            FROM events
            WHERE timestamp >= ? AND output_tokens BETWEEN 1 AND 200
            GROUP BY model
            """,
            (since_ts,),
        ).fetchall()

    for row in rows:
        model = row["model"]
        if model not in downgrades or row["turns"] < 20:
            continue
        alt = downgrades[model]
        est_current = row["cost"] or 0.0
        est_alt = cost_for(alt, int(row["in_t"] or 0), int(row["out_t"] or 0))
        delta_window = max(0.0, est_current - est_alt)
        # Scale to monthly estimate based on the window length.
        window_days = max(1.0, (time.time() - since_ts) / 86400)
        monthly = delta_window * 30 / window_days
        if monthly < 0.50:  # below the noise floor
            continue
        yield Suggestion(
            rule_id="expensive_model_small_output",
            severity="tip",
            title=f"Consider {alt} for short answers",
            body=(
                f"In the last {int(window_days)}d, {row['turns']} turns on "
                f"{model} produced <200 output tokens. Routing those to "
                f"{alt} would have cost about ${est_alt:.2f} instead of "
                f"${est_current:.2f}."
            ),
            action=f"Switch quick/cheap turns to {alt}; keep {model} for reasoning-heavy work.",
            estimated_monthly_savings_usd=round(monthly, 2),
            context={"model": model, "alternative": alt, "turns": row["turns"]},
        )


def rule_low_cache_usage(db_path: Path, since_ts: float) -> Iterable[Suggestion]:
    """Claude sessions with many turns but ~zero cache read/write.

    Prompt caching on Anthropic cuts the repeated-prefix cost by ~90%. If
    you're running multi-turn sessions with no cache tokens at all, you're
    almost certainly leaving money on the table.
    """
    with _conn(db_path) as c:
        rows = c.execute(
            """
            SELECT session_id,
                   COUNT(*) AS turns,
                   SUM(input_tokens)       AS in_t,
                   SUM(cache_read_tokens)  AS cache_r,
                   SUM(cache_write_tokens) AS cache_w,
                   SUM(cost_usd)           AS cost
            FROM events
            WHERE timestamp >= ? AND source = 'claude_code' AND session_id IS NOT NULL
            GROUP BY session_id
            HAVING turns >= 10 AND (cache_r + cache_w) = 0 AND in_t > 50000
            """,
            (since_ts,),
        ).fetchall()

    if not rows:
        return
    total_cost = sum(r["cost"] or 0 for r in rows)
    # If Claude's cache read is ~10% of base input price, assume 70% of the
    # input on long sessions could be cached — saves ~0.9 * 0.7 = ~63% on input.
    approx_savings = total_cost * 0.35
    window_days = max(1.0, (time.time() - since_ts) / 86400)
    monthly = approx_savings * 30 / window_days
    yield Suggestion(
        rule_id="low_cache_usage",
        severity="tip",
        title=f"Enable prompt caching on {len(rows)} Claude sessions",
        body=(
            f"{len(rows)} recent Claude Code sessions had 10+ turns with zero cache hits. "
            f"Cache read is ~10% of input price; most repeated prefixes (system prompt, "
            f"project context) should be marked cacheable."
        ),
        action="Add cache_control blocks to your system / long context prefixes, or upgrade to a client that enables caching by default.",
        estimated_monthly_savings_usd=round(monthly, 2),
        context={"sessions": len(rows), "window_cost_usd": round(total_cost, 2)},
    )


def rule_context_creep(db_path: Path, since_ts: float) -> Iterable[Suggestion]:
    """Sessions where input tokens per turn are growing monotonically.

    This is the classic 'I keep appending to the same chat' pattern —
    context grows, cost per turn grows with it. A fresh session or /compact
    is cheaper.
    """
    with _conn(db_path) as c:
        sessions = c.execute(
            """
            SELECT session_id, COUNT(*) AS turns
            FROM events
            WHERE timestamp >= ? AND session_id IS NOT NULL
            GROUP BY session_id
            HAVING turns >= 15
            """,
            (since_ts,),
        ).fetchall()

        bad = []
        for s in sessions:
            turns = c.execute(
                """SELECT input_tokens FROM events
                   WHERE session_id = ? AND timestamp >= ?
                   ORDER BY timestamp ASC""",
                (s["session_id"], since_ts),
            ).fetchall()
            xs = [t["input_tokens"] for t in turns]
            if not xs:
                continue
            first_quartile = sum(xs[: max(1, len(xs)//4)]) / max(1, len(xs)//4)
            last_quartile = sum(xs[-max(1, len(xs)//4):]) / max(1, len(xs)//4)
            if last_quartile > first_quartile * 3 and last_quartile > 20000:
                bad.append((s["session_id"], first_quartile, last_quartile, s["turns"]))

    for session_id, first, last, turns in bad[:5]:
        yield Suggestion(
            rule_id="context_creep",
            severity="warn",
            title="Long session with growing context",
            body=(
                f"Session {str(session_id)[:8]}… had {turns} turns and input "
                f"tokens per turn grew from ~{int(first):,} to ~{int(last):,}. "
                f"Cost per turn scales with context size."
            ),
            action="Use /compact, summarize, or start a fresh session once context passes ~50k tokens.",
            context={"session_id": session_id, "start_input": int(first), "end_input": int(last)},
        )


def rule_runaway_session_cost(db_path: Path, since_ts: float) -> Iterable[Suggestion]:
    with _conn(db_path) as c:
        rows = c.execute(
            """
            SELECT source, session_id, SUM(cost_usd) AS cost, COUNT(*) AS turns
            FROM events
            WHERE timestamp >= ? AND session_id IS NOT NULL
            GROUP BY source, session_id
            HAVING cost > 5.0
            ORDER BY cost DESC
            LIMIT 5
            """,
            (since_ts,),
        ).fetchall()
    for row in rows:
        yield Suggestion(
            rule_id="runaway_session",
            severity="warn",
            title=f"Expensive session: ${row['cost']:.2f}",
            body=(
                f"One {row['source']} session spent ${row['cost']:.2f} across "
                f"{row['turns']} turns. Single high-cost sessions usually "
                f"indicate context creep or model mismatch."
            ),
            action="Split the work into smaller sessions and/or drop to a cheaper model for scaffolding turns.",
            context={"session_id": row["session_id"], "source": row["source"], "cost_usd": row["cost"]},
        )


def rule_cross_tool_substitution(db_path: Path, since_ts: float) -> Iterable[Suggestion]:
    """If Gemini Flash handles similar-shape turns at a fraction of the cost,
    surface the comparison. Purely informational; you decide if it fits."""
    with _conn(db_path) as c:
        by_model = c.execute(
            """
            SELECT model,
                   AVG(input_tokens)  AS avg_in,
                   AVG(output_tokens) AS avg_out,
                   SUM(cost_usd)      AS cost,
                   COUNT(*)           AS turns
            FROM events WHERE timestamp >= ?
            GROUP BY model HAVING turns >= 20
            """,
            (since_ts,),
        ).fetchall()

    premium = [r for r in by_model if r["model"] in ("claude-opus-4-6", "gpt-5")]
    if not premium:
        return
    # Compare each premium against gemini-2.5-flash as a lower bound.
    cheap_model = "gemini-2.5-flash"
    if cheap_model not in PRICES:
        return
    for row in premium:
        est_cheap = cost_for(
            cheap_model,
            int(row["avg_in"] * row["turns"]),
            int(row["avg_out"] * row["turns"]),
        )
        if row["cost"] - est_cheap < 5.0:
            continue
        yield Suggestion(
            rule_id="cross_tool_comparison",
            severity="info",
            title=f"Gemini Flash benchmark for {row['model']}",
            body=(
                f"At the same token shape, {row['turns']} recent {row['model']} "
                f"turns would have cost about ${est_cheap:.2f} on {cheap_model} "
                f"vs ${row['cost']:.2f} as run. This isn't a recommendation — "
                f"just a price-floor comparison if the task tolerates the cheaper model."
            ),
            action=f"For non-reasoning turns, consider routing to {cheap_model}.",
            context={"model": row["model"], "alternative": cheap_model,
                     "actual_cost_usd": round(row["cost"], 2),
                     "alt_cost_usd": round(est_cheap, 2)},
        )


def rule_rapidfire_small(db_path: Path, since_ts: float) -> Iterable[Suggestion]:
    """Many small turns packed into short windows suggest batching/looping.

    We look at 60-second windows with >= 15 small (<500 output token) turns.
    """
    with _conn(db_path) as c:
        rows = c.execute(
            """
            SELECT CAST(timestamp / 60 AS INTEGER) AS min_bucket,
                   source,
                   COUNT(*) AS turns
            FROM events
            WHERE timestamp >= ? AND output_tokens < 500
            GROUP BY min_bucket, source
            HAVING turns >= 15
            ORDER BY turns DESC LIMIT 3
            """,
            (since_ts,),
        ).fetchall()
    for row in rows:
        yield Suggestion(
            rule_id="rapidfire_small",
            severity="tip",
            title=f"{row['turns']} rapid turns in a minute on {row['source']}",
            body=(
                f"Bursts of small turns often come from a loop that could be "
                f"batched into fewer larger prompts — each turn still pays the "
                f"full system-prompt cost."
            ),
            action="Batch similar requests into one prompt, or enable prompt caching so the repeated prefix is ~free.",
            context=dict(row),
        )


RULES = [
    rule_expensive_model_small_output,
    rule_low_cache_usage,
    rule_context_creep,
    rule_runaway_session_cost,
    rule_cross_tool_substitution,
    rule_rapidfire_small,
]


def generate(db_path: Path, days: float = 7.0) -> list[Suggestion]:
    since = time.time() - days * 86400
    out: list[Suggestion] = []
    for rule in RULES:
        try:
            for s in rule(db_path, since):
                out.append(s)
        except Exception:  # noqa: BLE001
            # One broken rule shouldn't blank out the whole page.
            continue
    # Sort: warnings first, then by estimated savings.
    sev = {"warn": 0, "tip": 1, "info": 2}
    out.sort(key=lambda s: (sev.get(s.severity, 3), -s.estimated_monthly_savings_usd))
    return out
