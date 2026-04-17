"""USD -> display-currency conversion.

Vendors publish prices in USD, so we store ``cost_usd`` as the source of truth
and convert at display time. The rate is fetched from Frankfurter
(https://www.frankfurter.app/) — ECB reference rates, no key, no rate limit —
and cached to disk for six hours.

If the network is unreachable and no cache exists, we fall back to a
user-configurable rate. The fetch always returns *something* usable so the
UI doesn't blank out when you're offline.

Thread-safety: one FxService per process; refresh is guarded by a lock.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


log = logging.getLogger(__name__)

# Frankfurter migrated from .app -> .dev in 2026. We use the new canonical URL
# and also set follow_redirects=True so we're resilient if they move again.
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
REFRESH_SECONDS = 6 * 3600
# How long stale cache is still considered 'usable' before we start warning.
MAX_USABLE_CACHE_S = 30 * 86400

SYMBOLS = {
    "EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥",
    "CHF": "CHF", "SEK": "kr", "NOK": "kr", "DKK": "kr",
    "CAD": "CA$", "AUD": "A$", "NZD": "NZ$",
}


def symbol_for(currency: str) -> str:
    return SYMBOLS.get(currency.upper(), currency.upper() + " ")


def format_money(amount: float, currency: str, decimals: int = 2) -> str:
    """Keep it simple and locale-free: ``€1.23``, ``$1.23``, ``CHF 1.23``."""
    sym = symbol_for(currency)
    if sym.endswith(" "):  # 3-letter fallback
        return f"{sym}{amount:,.{decimals}f}"
    return f"{sym}{amount:,.{decimals}f}"


@dataclass
class Rate:
    """One cached rate."""
    target: str            # e.g. "EUR"
    usd_per_target: float  # multiply a USD amount by this to get target
    as_of: float           # unix seconds when fetched
    source: str            # "frankfurter", "cache", "fallback", "override"


class FxService:
    """Thread-safe service for fetching/caching USD->target conversion rates."""

    def __init__(
        self,
        cache_path: Path,
        fallback_rate: float = 0.92,
        override_rate: float | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.fallback_rate = fallback_rate
        self.override_rate = override_rate
        self._lock = threading.Lock()
        self._current: dict[str, Rate] = {}
        self._last_refresh: dict[str, float] = {}

    # --- public API -----------------------------------------------------

    def get_rate(self, target: str) -> Rate:
        """Return a usable rate for USD -> ``target``. Refreshes if stale."""
        target = target.upper()
        if target == "USD":
            return Rate("USD", 1.0, time.time(), "identity")
        if self.override_rate is not None:
            return Rate(target, self.override_rate, time.time(), "override")

        with self._lock:
            now = time.time()
            last = self._last_refresh.get(target, 0.0)
            if target not in self._current or (now - last) >= REFRESH_SECONDS:
                self._refresh(target)
            return self._current.get(
                target,
                Rate(target, self.fallback_rate, now, "fallback"),
            )

    def convert(self, amount_usd: float, target: str) -> float:
        return round(amount_usd * self.get_rate(target).usd_per_target, 6)

    # --- internals ------------------------------------------------------

    def _refresh(self, target: str) -> None:
        """Best-effort refresh: network, then disk cache, then fallback."""
        now = time.time()
        cached = self._load_cache().get(target)

        try:
            with httpx.Client(timeout=5.0, follow_redirects=True) as client:
                r = client.get(FRANKFURTER_URL, params={"base": "USD", "symbols": target})
                r.raise_for_status()
                data = r.json()
                rate_val = float(data["rates"][target])
                self._current[target] = Rate(target, rate_val, now, "frankfurter")
                self._last_refresh[target] = now
                self._save_cache(target, rate_val, now)
                return
        except Exception as e:  # noqa: BLE001
            log.info("fx: Frankfurter unreachable (%s); falling back", e)

        if cached is not None:
            age = now - cached["as_of"]
            if age < MAX_USABLE_CACHE_S:
                self._current[target] = Rate(
                    target, float(cached["rate"]), float(cached["as_of"]), "cache"
                )
                self._last_refresh[target] = now  # hold off retrying for a bit
                return
            log.warning("fx: cache is %.0fd stale for %s; using fallback", age / 86400, target)

        self._current[target] = Rate(target, self.fallback_rate, now, "fallback")
        self._last_refresh[target] = now

    def _load_cache(self) -> dict:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _save_cache(self, target: str, rate_val: float, as_of: float) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load_cache()
        data[target] = {"rate": rate_val, "as_of": as_of}
        try:
            self.cache_path.write_text(json.dumps(data))
        except OSError as e:
            log.warning("fx: failed to write cache: %s", e)


# --------- helper for decorating API payloads ------------------------------

def attach_money(obj, rate: Rate) -> None:
    """Walk a dict/list and inject ``cost`` alongside every ``cost_usd``.

    We mutate in place — callers pass a fresh dict built from DB rows, so this
    is safe.
    """
    if isinstance(obj, dict):
        if "cost_usd" in obj and "cost" not in obj:
            obj["cost"] = round(float(obj["cost_usd"] or 0) * rate.usd_per_target, 6)
        for v in obj.values():
            attach_money(v, rate)
    elif isinstance(obj, list):
        for item in obj:
            attach_money(item, rate)
