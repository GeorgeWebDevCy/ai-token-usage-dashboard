"""System tray icon.

pystray works on Windows (win32), macOS (Cocoa via Rumps-like bindings), and
most Linux DEs (AppIndicator/XEmbed). On headless Linux the whole tray can
fail to initialize — we handle that by logging and continuing: the web UI
alone is still useful.

Tooltip shows today's token count; clicking the 'Open dashboard' menu item
launches the browser to the configured host:port.
"""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

from .db import Database
from .fx import FxService, format_money

log = logging.getLogger(__name__)


def _icon_image(color: str = "#7aa2f7") -> Image.Image:
    """Generate a 64x64 icon with 'Ai' text — no external assets needed."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=12, fill=color)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 32)
    except OSError:
        font = ImageFont.load_default()
    d.text((13, 14), "AI", fill="white", font=font)
    return img


def _format_count(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}k"
    return f"{n/1_000_000:.2f}M"


def run_tray(
    db: Database,
    dashboard_url: str,
    on_quit: Callable[[], None],
    fx: FxService | None = None,
    currency: str = "EUR",
) -> None:
    """Run the pystray icon on the current thread.

    pystray wants to own the main thread on macOS, so this should be called
    from __main__ after starting the server + collectors on background threads.
    """
    try:
        import pystray
    except Exception as e:  # noqa: BLE001
        log.warning("pystray unavailable (%s); tray disabled", e)
        return

    def _today_tokens() -> int:
        import datetime as dt
        start = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        t = db.totals_since(start)
        return int(
            (t["input_tokens"] or 0)
            + (t["output_tokens"] or 0)
            + (t["cache_read_tokens"] or 0)
            + (t["cache_write_tokens"] or 0)
        )

    def _today_cost_display() -> str:
        import datetime as dt
        start = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        cost_usd = float(db.totals_since(start)["cost_usd"] or 0.0)
        if fx is not None:
            cost = fx.convert(cost_usd, currency)
            return format_money(cost, currency)
        return format_money(cost_usd, "USD")

    def _refresh_tooltip(icon: "pystray.Icon") -> None:
        while True:
            try:
                n = _today_tokens()
                icon.title = f"AI tokens today: {_format_count(n)}  (~{_today_cost_display()})"
            except Exception as e:  # noqa: BLE001
                log.debug("tray refresh: %s", e)
            time.sleep(5.0)

    def _open(icon, item):
        webbrowser.open(dashboard_url)

    def _quit(icon, item):
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", _open, default=True),
        pystray.MenuItem(lambda i: f"Today: {_format_count(_today_tokens())} tokens", None, enabled=False),
        pystray.MenuItem(lambda i: f"Today cost: ~{_today_cost_display()}",            None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )

    icon = pystray.Icon("ai-token-dashboard", _icon_image(), "AI Token Dashboard", menu)
    threading.Thread(target=_refresh_tooltip, args=(icon,), daemon=True).start()
    icon.run()
