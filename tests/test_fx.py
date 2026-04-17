"""FX service + money decoration tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_token_dashboard.fx import FxService, attach_money, format_money


@pytest.fixture()
def cache(tmp_path):
    return tmp_path / "fx.json"


# --- FxService ------------------------------------------------------------

def test_override_short_circuits_network(cache):
    fx = FxService(cache, fallback_rate=0.5, override_rate=1.23)
    rate = fx.get_rate("EUR")
    assert rate.usd_per_target == 1.23
    assert rate.source == "override"


def test_usd_to_usd_is_identity(cache):
    fx = FxService(cache, override_rate=0.5)
    rate = fx.get_rate("USD")
    assert rate.usd_per_target == 1.0
    assert rate.source == "identity"


def test_falls_back_when_offline_and_no_cache(cache, monkeypatch):
    fx = FxService(cache, fallback_rate=0.91)

    def _fail(*a, **kw):
        raise RuntimeError("offline")

    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    rate = fx.get_rate("EUR")
    assert rate.usd_per_target == 0.91
    assert rate.source == "fallback"


def test_uses_cache_when_offline(cache, monkeypatch):
    # Seed a cache file as if we'd successfully fetched an hour ago.
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"EUR": {"rate": 0.95, "as_of": time.time() - 3600}}))
    fx = FxService(cache, fallback_rate=0.80)

    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    rate = fx.get_rate("EUR")
    assert rate.usd_per_target == 0.95
    assert rate.source == "cache"


def test_convert_uses_rate(cache):
    fx = FxService(cache, override_rate=0.5)
    assert fx.convert(10.0, "EUR") == 5.0


# --- attach_money ---------------------------------------------------------

def test_attach_money_injects_cost_field():
    from ai_token_dashboard.fx import Rate
    payload = {
        "totals": {"cost_usd": 1.0, "input_tokens": 100},
        "rows": [{"cost_usd": 2.0}, {"cost_usd": 0.0}],
    }
    attach_money(payload, Rate("EUR", 0.5, time.time(), "override"))
    assert payload["totals"]["cost"] == 0.5
    assert payload["rows"][0]["cost"] == 1.0
    assert payload["rows"][1]["cost"] == 0.0
    assert "cost_usd" in payload["totals"]  # original preserved


def test_attach_money_idempotent_when_cost_present():
    from ai_token_dashboard.fx import Rate
    payload = {"cost_usd": 1.0, "cost": 999.0}  # already set
    attach_money(payload, Rate("EUR", 0.5, time.time(), "override"))
    assert payload["cost"] == 999.0  # left alone


# --- formatting -----------------------------------------------------------

def test_format_money_eur_uses_euro_symbol():
    assert format_money(1.23, "EUR") == "€1.23"


def test_format_money_usd_uses_dollar_sign():
    assert format_money(1.23, "USD") == "$1.23"


def test_format_money_unknown_currency_uses_code():
    assert "PLN" in format_money(1.23, "PLN")
