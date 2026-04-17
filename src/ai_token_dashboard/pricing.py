"""Best-effort $/million-token price table.

Prices drift. This is a local estimate, not a bill. The user can override or
extend by editing this file; costs are re-computed at insertion time from the
``cost_for`` helper so there's no stored drift.

Structure: price["input"] and price["output"] are $ per 1M tokens. Cache
read/write rates are applied when the vendor reports cache hits.
"""

from __future__ import annotations

# Rough public list prices as of 2026-04. Update as pricing changes.
# Anthropic numbers cross-checked against the phuryn/claude-usage project's
# pricing table (also sourced from Anthropic's public docs in April 2026).
PRICES: dict[str, dict[str, float]] = {
    # Anthropic (verify against https://docs.claude.com before billing anything)
    "claude-opus-4-6":       {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-sonnet-4-6":     {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-5":      {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
    # OpenAI (ballpark — verify against your dashboard)
    "gpt-5":                 {"input":  5.00, "output": 15.00},
    "gpt-5-mini":            {"input":  0.25, "output":  2.00},
    "o3":                    {"input":  2.00, "output":  8.00},
    # Google
    "gemini-2.5-pro":        {"input":  1.25, "output":  5.00},
    "gemini-2.5-flash":      {"input":  0.075, "output":  0.30},
}


def cost_for(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Return estimated $ cost for a single turn. Unknown model => 0."""
    p = _resolve(model)
    if not p:
        return 0.0
    total = 0.0
    total += input_tokens      * p.get("input", 0.0)      / 1_000_000
    total += output_tokens     * p.get("output", 0.0)     / 1_000_000
    total += cache_read_tokens * p.get("cache_read", 0.0) / 1_000_000
    total += cache_write_tokens* p.get("cache_write", 0.0)/ 1_000_000
    return round(total, 6)


def _resolve(model: str) -> dict[str, float] | None:
    """Match a specific model id or a prefix (e.g. 'claude-sonnet-4-6-20260101')."""
    if model in PRICES:
        return PRICES[model]
    # Fall back to longest-prefix match so dated model ids still resolve.
    matches = [name for name in PRICES if model.startswith(name)]
    if matches:
        return PRICES[max(matches, key=len)]
    return None
