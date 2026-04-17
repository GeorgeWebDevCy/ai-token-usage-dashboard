"""FastAPI HTTP + WebSocket server.

Endpoints:
  GET  /api/stats/today      — today's aggregate totals
  GET  /api/stats/range      — totals for an arbitrary window (?hours= or ?days=)
  GET  /api/stats/by_source  — per-source breakdown
  GET  /api/stats/by_model   — per-model breakdown
  GET  /api/stats/series     — bucketed time series for the chart
  GET  /api/events/recent    — most recent N raw events
  GET  /api/suggestions      — advisor tips with converted savings
  GET  /api/fx               — current exchange rate info
  WS   /ws                   — live TokenEvent stream

Every response that includes a ``cost_usd`` field is enriched in-place with a
parallel ``cost`` field in the configured display currency, and the top-level
response includes ``currency`` and ``rate`` so the client can format and label
numbers without a second round-trip.

The dashboard is served statically at ``/`` from the package's ``web/`` folder.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import advisor
from .config import DisplayConfig
from .db import Database
from .events import BUS
from .fx import FxService, attach_money


def _start_of_today_ts() -> float:
    import datetime as dt
    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today.timestamp()


def create_app(db: Database, display: DisplayConfig, fx: FxService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="AI Token Dashboard", lifespan=lifespan)

    def _enrich(payload: dict) -> dict:
        """Attach currency/rate to the envelope and inject ``cost`` fields."""
        rate = fx.get_rate(display.currency)
        attach_money(payload, rate)
        return {
            "currency": display.currency,
            "rate": rate.usd_per_target,
            "rate_source": rate.source,
            "rate_as_of": rate.as_of,
            **payload,
        }

    # -------- REST ---------------------------------------------------------

    @app.get("/api/stats/today")
    def stats_today():
        since = _start_of_today_ts()
        return _enrich({
            "since_ts": since,
            "totals": db.totals_since(since),
            "by_source": db.by_source_since(since),
        })

    @app.get("/api/stats/range")
    def stats_range(hours: float | None = None, days: float | None = None):
        if hours is not None:
            window_s = hours * 3600
        elif days is not None:
            window_s = days * 86400
        else:
            window_s = 24 * 3600
        since = time.time() - window_s
        return _enrich({
            "since_ts": since,
            "totals": db.totals_since(since),
            "by_source": db.by_source_since(since),
        })

    @app.get("/api/stats/by_source")
    def stats_by_source(days: float = 7):
        since = time.time() - days * 86400
        return _enrich({"since_ts": since, "rows": db.by_source_since(since)})

    @app.get("/api/stats/by_model")
    def stats_by_model(days: float = 7):
        since = time.time() - days * 86400
        return _enrich({"since_ts": since, "rows": db.by_model_since(since)})

    @app.get("/api/stats/series")
    def stats_series(hours: float = 24, bucket_minutes: int = 15):
        since = time.time() - hours * 3600
        return {
            "since_ts": since,
            "bucket_seconds": bucket_minutes * 60,
            "rows": db.buckets_since(since, bucket_minutes * 60),
        }

    @app.get("/api/events/recent")
    def events_recent(limit: int = 100):
        return _enrich({"events": db.recent(limit=min(limit, 1000))})

    @app.get("/api/suggestions")
    def suggestions(days: float = 7.0):
        items = advisor.generate(db.path, days=days)
        rate = fx.get_rate(display.currency)
        payload_items = []
        total_savings_usd = 0.0
        for s in items:
            d = s.to_dict()
            d["estimated_monthly_savings"] = round(
                s.estimated_monthly_savings_usd * rate.usd_per_target, 2
            )
            payload_items.append(d)
            total_savings_usd += s.estimated_monthly_savings_usd
        return _enrich({
            "days": days,
            "count": len(items),
            "estimated_monthly_savings_usd": round(total_savings_usd, 2),
            "estimated_monthly_savings": round(total_savings_usd * rate.usd_per_target, 2),
            "items": payload_items,
        })

    @app.get("/api/fx")
    def fx_info():
        rate = fx.get_rate(display.currency)
        return {
            "currency": display.currency,
            "rate": rate.usd_per_target,
            "source": rate.source,
            "as_of": rate.as_of,
        }

    # -------- WebSocket ---------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            async for ev in BUS.subscribe():
                rate = fx.get_rate(display.currency)
                data = ev.to_dict()
                data["cost"] = round((data.get("cost_usd") or 0) * rate.usd_per_target, 6)
                await websocket.send_json({
                    "type": "event",
                    "currency": display.currency,
                    "rate": rate.usd_per_target,
                    "data": data,
                })
        except WebSocketDisconnect:
            return
        except Exception:
            await websocket.close()

    # -------- static dashboard -------------------------------------------

    web_root = _web_root()
    if web_root and web_root.exists():
        app.mount("/web", StaticFiles(directory=str(web_root)), name="web")

        @app.get("/")
        def index():
            return FileResponse(str(web_root / "index.html"))

    return app


def _web_root() -> Path | None:
    try:
        root = files("ai_token_dashboard") / "web"
        return Path(str(root))
    except Exception:
        return None
