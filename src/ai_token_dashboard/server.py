"""FastAPI HTTP + WebSocket server."""

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


def _resolve_window(
    hours: float | None,
    days: float | None,
    since_ts: float | None,
    until_ts: float | None,
    default_hours: float = 24,
) -> tuple[float, float | None]:
    """Return (since_ts, until_ts) from whichever params were supplied."""
    if since_ts is not None:
        return since_ts, until_ts
    if hours is not None:
        return time.time() - hours * 3600, until_ts
    if days is not None:
        return time.time() - days * 86400, until_ts
    return time.time() - default_hours * 3600, until_ts


def create_app(db: Database, display: DisplayConfig, fx: FxService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="AI Token Dashboard", lifespan=lifespan)

    def _enrich(payload: dict) -> dict:
        rate = fx.get_rate(display.currency)
        attach_money(payload, rate)
        return {
            "currency": display.currency,
            "rate": rate.usd_per_target,
            "rate_source": rate.source,
            "rate_as_of": rate.as_of,
            **payload,
        }

    @app.get("/api/stats/today")
    def stats_today():
        since = _start_of_today_ts()
        return _enrich({
            "since_ts": since,
            "totals": db.totals_since(since),
            "by_source": db.by_source_since(since),
        })

    @app.get("/api/stats/range")
    def stats_range(
        hours: float | None = None,
        days: float | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ):
        s, u = _resolve_window(hours, days, since_ts, until_ts)
        return _enrich({
            "since_ts": s,
            "until_ts": u,
            "totals": db.totals_since(s, u),
            "by_source": db.by_source_since(s, u),
        })

    @app.get("/api/stats/by_source")
    def stats_by_source(
        days: float = 7,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ):
        s, u = _resolve_window(None, days, since_ts, until_ts, default_hours=days * 24)
        return _enrich({"since_ts": s, "until_ts": u, "rows": db.by_source_since(s, u)})

    @app.get("/api/stats/by_model")
    def stats_by_model(
        days: float = 7,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ):
        s, u = _resolve_window(None, days, since_ts, until_ts, default_hours=days * 24)
        return _enrich({"since_ts": s, "until_ts": u, "rows": db.by_model_since(s, u)})

    @app.get("/api/stats/by_project")
    def stats_by_project(
        days: float = 7,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ):
        s, u = _resolve_window(None, days, since_ts, until_ts, default_hours=days * 24)
        rows = db.by_project_since(s, u)
        import re, os
        home_slug = re.sub(r"[^a-zA-Z0-9]", "-", str(Path.home()))
        def decode_slug(slug: str) -> str:
            if slug == "unknown":
                return slug
            # Strip home dir prefix → ~/remainder
            if slug.lower().startswith(home_slug.lower()):
                rest = slug[len(home_slug):].lstrip("-")
                slug = "~/" + rest
            else:
                # Drive letter: X-- → X:/
                slug = re.sub(r"^([a-zA-Z])--", lambda m: m.group(1).upper() + ":/", slug)
            # Remaining --  → / then - → /
            slug = slug.replace("--", "/").replace("-", "/")
            # Normalize double slashes (except after drive)
            slug = re.sub(r"(?<!:)//+", "/", slug)
            return slug
        for r in rows:
            r["project_display"] = decode_slug(r["project"])
        return _enrich({"since_ts": s, "until_ts": u, "rows": rows})

    @app.get("/api/stats/series")
    def stats_series(
        hours: float = 24,
        bucket_minutes: int = 15,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ):
        s, u = _resolve_window(hours, None, since_ts, until_ts, default_hours=hours)
        return {
            "since_ts": s,
            "until_ts": u,
            "bucket_seconds": bucket_minutes * 60,
            "rows": db.buckets_since(s, bucket_minutes * 60, u),
        }

    @app.get("/api/events/recent")
    def events_recent(
        limit: int = 100,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ):
        return _enrich({"events": db.recent(limit=min(limit, 1000), since_ts=since_ts, until_ts=until_ts)})

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
            "count": len(payload_items),
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

    @app.get("/api/meta")
    def meta():
        return {"earliest_ts": db.earliest_ts(), "row_count": db.row_count()}

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
