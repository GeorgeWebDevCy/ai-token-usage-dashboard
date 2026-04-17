# AI Token Usage Dashboard

A local, cross-platform (Windows / Linux / macOS) desktop tool that tracks your
AI token usage in real time across multiple tools — Claude Code, OpenAI Codex,
Gemini CLI, and any vendor whose usage API you care to poll.

Runs in the system tray, opens a live web dashboard on click, and surfaces
**suggestions** on how to reduce your token spend based on your actual usage.

- Tray icon shows today's tokens & cost at a glance
- Local web dashboard at `http://127.0.0.1:8765/` with live WebSocket updates
- Rule-based advisor flags model downgrades, missed caching, runaway sessions
- Everything local — no cloud, no account — data in SQLite at `~/.ai-token-dashboard/tokens.db`

## Install

Requires Python 3.10+.

```bash
git clone <this-repo>
cd ai-token-usage-dashboard
pip install -e .
```

## Run

```bash
ai-token-dashboard            # tray + dashboard daemon (default)
ai-token-dashboard --open     # daemon + opens the browser
ai-token-dashboard --no-tray  # server only (headless)
```

One-shot subcommands (no daemon required):

```bash
ai-token-dashboard today           # today's totals by model
ai-token-dashboard stats --days 30 # N-day summary + top advisor tips
ai-token-dashboard scan            # backfill: ingest all existing JSONL history
```

`scan` is the one you want after installing — it walks every configured
session-log directory and populates the DB with everything you've already
spent. Inserts are idempotent, so it's safe to rerun.

First run writes a default config to `~/.ai-token-dashboard/config.yaml`. Edit
it to enable/disable collectors, change the port, or plug in admin API keys
for the vendor billing endpoints.

## What it tracks

| Source            | How it works                                                  | Needs a key? |
|-------------------|---------------------------------------------------------------|--------------|
| Claude Code CLI   | Tails `~/.claude/projects/**/*.jsonl` with a filesystem watcher | No           |
| OpenAI Codex CLI  | Tails `~/.codex/sessions/*.jsonl` (also `~/.openai/…`)         | No           |
| Gemini CLI        | Tails `~/.gemini/tmp/*.jsonl`                                  | No           |
| Anthropic Admin API | Polls the org usage endpoint on a configurable interval     | Yes (admin key) |
| OpenAI Usage API  | Polls `/v1/organization/usage/completions`                    | Yes (admin key) |
| Google Usage      | Placeholder (no per-user endpoint at time of writing)         | –            |

File-watchers pick up new tokens within a second of a response completing.
Billing APIs reconcile anything not covered by the CLIs (web, desktop app,
other machines).

## Cost estimates

`pricing.py` carries a table of `$/M tokens` for each supported model (input,
output, cache read, cache write where applicable). Costs are recomputed on
insertion, so when vendors move prices you just edit the file.

## Currency

Vendors publish prices in USD, so the source-of-truth `cost_usd` stays in the
database. At display time, every cost is converted to your configured
currency (default **EUR**). The exchange rate comes from [Frankfurter](https://www.frankfurter.dev)
(ECB reference rates, no API key, no rate limit), cached locally for 6 hours
at `~/.ai-token-dashboard/fx_cache.json`.

Every API response with costs includes a top-level `currency`, `rate`, and
`rate_source` so the dashboard can label numbers correctly. The WebSocket
live stream does the same on each event.

Override in `config.yaml`:

```yaml
display:
  currency: EUR              # GBP, USD, CHF, JPY … any ISO 4217 code
  usd_rate_override: null    # pin a specific rate and skip network entirely
  usd_rate_fallback: 0.92    # used only if Frankfurter is unreachable and no cache exists
```

`GET /api/fx` returns the current rate and source (`frankfurter` | `cache` |
`fallback` | `override`).

## Savings advisor

The `/api/suggestions` endpoint (and the Savings panel on the dashboard) runs
a handful of rules over your recent data:

- **expensive_model_small_output** — premium models doing short/cheap turns; suggests the cheaper sibling with $ saved/mo
- **low_cache_usage** — multi-turn Claude sessions with zero cache hits
- **context_creep** — sessions where input tokens per turn are growing
- **runaway_session** — any single session over $5
- **cross_tool_comparison** — what the same token shape would cost on Gemini Flash
- **rapidfire_small** — bursts of small turns that would benefit from batching

Rules live in `src/ai_token_dashboard/advisor.py` — add your own heuristics
there.

## Config reference

```yaml
server:
  host: 127.0.0.1
  port: 8765
database:
  path: ~/.ai-token-dashboard/tokens.db
collectors:
  claude_code:
    enabled: true
    paths: ["~/.claude/projects"]
  codex:
    enabled: true
    paths: ["~/.codex/sessions", "~/.openai/sessions"]
  gemini:
    enabled: true
    paths: ["~/.gemini/tmp"]
  anthropic_api:
    enabled: false
    api_key_env: ANTHROPIC_ADMIN_KEY
    poll_interval_s: 300
  openai_api:
    enabled: false
    api_key_env: OPENAI_ADMIN_KEY
    poll_interval_s: 300
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Architecture

```
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │ Claude Code  │   │ Codex CLI    │   │ Gemini CLI   │   JSONL session logs
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          ▼                  ▼                  ▼
   ┌──────────────────────────────────────────────────┐
   │           watchdog file-tail collectors          │
   └──────┬───────────────────────────────────────────┘
          ▼                            ┌──────────────────┐
   ┌──────────────┐                    │ Anthropic/OpenAI │
   │  Event bus   │◀───── polling ◀────│  billing APIs    │
   └──────┬───────┘                    └──────────────────┘
          ▼
   ┌──────────────┐        ┌─────────────────┐
   │  SQLite DB   │───────▶│ FastAPI + WS    │──▶ browser dashboard
   └──────────────┘        └─────────────────┘
          │                         │
          └──────── advisor ────────┘
                     │
                     ▼
              pystray tray icon
```

## Where each CLI stores session logs (defaults)

| OS       | Claude Code                                | Codex CLI                           | Gemini CLI                   |
|----------|--------------------------------------------|-------------------------------------|------------------------------|
| macOS    | `~/.claude/projects/<slug>/<uuid>.jsonl`   | `~/.codex/sessions/`                | `~/.gemini/tmp/`             |
| Linux    | `~/.claude/projects/…`                     | `~/.codex/sessions/` or `~/.config/openai/sessions/` | `~/.gemini/tmp/` or `~/.config/gemini/sessions/` |
| Windows  | `%USERPROFILE%\.claude\projects\…`         | `%USERPROFILE%\.codex\sessions\`    | `%USERPROFILE%\.gemini\tmp\` |

If your install uses a different path, add it to `config.yaml`.
