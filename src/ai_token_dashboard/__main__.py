"""Entry point.

Responsibilities, in order:
  1. Load config (create default file on first run).
  2. Open the SQLite database.
  3. Start an asyncio loop in a background thread for async collectors + FastAPI.
  4. Start the file-watcher collectors (they use watchdog's own threads).
  5. Start uvicorn inside the background loop.
  6. Run the pystray tray icon on the main thread (blocking).
  7. On tray quit, shut everything down.

If pystray can't initialize (headless Linux, etc.) the server still runs and
the user can use the web dashboard alone — we print the URL to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
import webbrowser
from pathlib import Path

from .config import AppConfig, DEFAULT_APP_DIR
from .db import Database
from .fx import FxService
from .collectors.base import JsonlTailCollector
from .collectors.claude_code import ClaudeCodeCollector
from .collectors.codex import CodexCollector
from .collectors.gemini import GeminiCollector
from .collectors.billing_api import AnthropicApiCollector, OpenAIApiCollector, GoogleApiCollector
from .server import create_app


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ai_token_dashboard")


def _start_loop_in_thread() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="asyncio-loop", daemon=True)
    t.start()
    return loop


def _start_collectors(
    cfg: AppConfig, db: Database, loop: asyncio.AbstractEventLoop
) -> list[JsonlTailCollector]:
    started: list[JsonlTailCollector] = []

    file_types: dict[str, type[JsonlTailCollector]] = {
        "claude_code": ClaudeCodeCollector,
        "codex":       CodexCollector,
        "gemini":      GeminiCollector,
    }
    for name, cls in file_types.items():
        c = cfg.collectors.get(name)
        if not c or not c.enabled:
            continue
        coll = cls(paths=c.resolved_paths, db=db, loop=loop)
        try:
            coll.start()
            started.append(coll)
        except Exception as e:  # noqa: BLE001
            log.warning("failed to start %s collector: %s", name, e)

    api_types = {
        "anthropic_api": AnthropicApiCollector,
        "openai_api":    OpenAIApiCollector,
        "google_api":    GoogleApiCollector,
    }
    for name, cls in api_types.items():
        c = cfg.collectors.get(name)
        if not c or not c.enabled:
            continue
        if not c.api_key:
            log.info("%s enabled but %s not set; skipping", name, c.api_key_env)
            continue
        poller = cls(db=db, app_dir=DEFAULT_APP_DIR, api_key=c.api_key, interval_s=c.poll_interval_s)
        asyncio.run_coroutine_threadsafe(
            _start_poller(poller, loop), loop
        )

    return started


async def _start_poller(poller, loop):
    poller.start(loop)


def _start_server(
    cfg: AppConfig, db: Database, fx: FxService, loop: asyncio.AbstractEventLoop
) -> None:
    import uvicorn

    app = create_app(db, cfg.display, fx)
    config = uvicorn.Config(
        app, host=cfg.server.host, port=cfg.server.port,
        log_level="warning", access_log=False, loop="asyncio",
    )
    server = uvicorn.Server(config)

    async def run():
        await server.serve()

    asyncio.run_coroutine_threadsafe(run(), loop)


def main() -> int:
    # If the user passed a subcommand (today/stats/scan), dispatch there.
    # Otherwise fall through to the daemon flags below.
    from . import cli
    rc = cli.dispatch(sys.argv[1:])
    if rc != -1:
        return rc

    ap = argparse.ArgumentParser(
        prog="ai-token-dashboard",
        description="Run the tray + dashboard daemon. "
                    "For one-shot queries see: today | stats | scan",
    )
    ap.add_argument("--no-tray", action="store_true", help="Run server only, skip tray")
    ap.add_argument("--open",   action="store_true", help="Open the dashboard in a browser on start")
    ap.add_argument("--config", type=Path, default=None, help="Override config file path")
    args = ap.parse_args()

    cfg = AppConfig.load(args.config)
    db = Database(cfg.db_path)
    fx = FxService(
        cache_path=DEFAULT_APP_DIR / "fx_cache.json",
        fallback_rate=cfg.display.usd_rate_fallback,
        override_rate=cfg.display.usd_rate_override,
    )

    loop = _start_loop_in_thread()
    watchers = _start_collectors(cfg, db, loop)
    _start_server(cfg, db, fx, loop)

    dashboard_url = f"http://{cfg.server.host}:{cfg.server.port}/"
    print(f"AI Token Dashboard running at {dashboard_url}")
    print(f"  DB:     {cfg.db_path}")
    print(f"  Config: {args.config or (DEFAULT_APP_DIR / 'config.yaml')}")
    if args.open:
        webbrowser.open(dashboard_url)

    # Graceful shutdown on Ctrl-C even without tray.
    stop_event = threading.Event()

    def _shutdown(*_):
        for w in watchers:
            try:
                w.stop()
            except Exception:  # noqa: BLE001
                pass
        loop.call_soon_threadsafe(loop.stop)
        stop_event.set()

    signal.signal(signal.SIGINT, lambda *a: _shutdown())
    signal.signal(signal.SIGTERM, lambda *a: _shutdown())

    if args.no_tray:
        try:
            stop_event.wait()
        except KeyboardInterrupt:
            _shutdown()
        return 0

    from .tray import run_tray
    try:
        run_tray(db, dashboard_url, on_quit=_shutdown,
                 fx=fx, currency=cfg.display.currency)
    except Exception as e:  # noqa: BLE001
        log.warning("tray failed (%s); continuing headless", e)
        try:
            stop_event.wait()
        except KeyboardInterrupt:
            _shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
