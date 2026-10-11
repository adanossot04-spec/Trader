"""Джерела добових свічок: розбір відповідей, кеш, фолбек, календар сесій."""

from __future__ import annotations

import datetime as dt
import json
from datetime import date

import pytest

from app import market
from app.market import Bar, MarketData, fetch_daily, next_session_date, next_weekday, synthetic_bars


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)[:200]

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def yahoo_payload(days: int = 150) -> dict:
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    stamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for idx in range(days):
        stamp = start + dt.timedelta(days=idx)
        if stamp.weekday() >= 5:
            continue
        price = 100 + idx
        stamps.append(int(stamp.timestamp()))
        opens.append(price)
        highs.append(price + 1.5)
        lows.append(price - 1.5)
        closes.append(price + 0.5)
        volumes.append(1_000_000 + idx)
    return {
        "chart": {
            "result": [
                {
                    "timestamp": stamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens, "high": highs, "low": lows,
                                "close": closes, "volume": volumes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


# ─── розбір відповідей ────────────────────────────────────────────────────────

def test_fetch_yahoo_parses_bars(monkeypatch):
    monkeypatch.setattr(market.httpx, "get", lambda *a, **k: FakeResponse(yahoo_payload()))
    monkeypatch.setattr(market, "session_is_open", lambda *a, **k: False)
    bars = market.fetch_yahoo("NVDA", lookback_days=500)
    expected = sum(1 for idx in range(150) if (dt.datetime(2026, 1, 1) + dt.timedelta(days=idx)).weekday() < 5)
    assert len(bars) == expected >= 60
    assert bars[0].date < bars[-1].date
    assert bars[-1].close > bars[-1].low


def test_fetch_yahoo_drops_partial_session(monkeypatch):
    payload = yahoo_payload()
    today = market.now_et().date()
    payload["chart"]["result"][0]["timestamp"].append(int(dt.datetime.combine(today, dt.time(15, 0), tzinfo=market.ET).timestamp()))
    quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
    for key in ("open", "high", "low", "close", "volume"):
        quote[key].append(quote[key][-1])
    monkeypatch.setattr(market.httpx, "get", lambda *a, **k: FakeResponse(payload))
    monkeypatch.setattr(market, "session_is_open", lambda *a, **k: True)
    bars = market.fetch_yahoo("NVDA", lookback_days=500)
    assert bars[-1].date != today.isoformat()


def test_fetch_yahoo_rejects_short_history(monkeypatch):
    monkeypatch.setattr(market.httpx, "get", lambda *a, **k: FakeResponse(yahoo_payload(days=30)))
    monkeypatch.setattr(market, "session_is_open", lambda *a, **k: False)
    with pytest.raises(ValueError):
        market.fetch_yahoo("NVDA", lookback_days=500)


def test_fetch_twelvedata_parses(monkeypatch):
    values = [
        {"datetime": f"2026-01-{idx + 1:02d}", "open": "10", "high": "11", "low": "9", "close": "10.5", "volume": "1000"}
        for idx in range(70)
    ]
    monkeypatch.setattr(market.httpx, "get", lambda *a, **k: FakeResponse({"status": "ok", "values": values}))
    bars = market.fetch_twelvedata("AAPL", lookback_days=500, api_key="x")
    assert len(bars) == 70 and bars[0].close == 10.5


def test_fetch_twelvedata_reports_api_error(monkeypatch):
    monkeypatch.setattr(
        market.httpx, "get", lambda *a, **k: FakeResponse({"status": "error", "message": "API limit reached"})
    )
    with pytest.raises(ValueError) as exc:
        market.fetch_twelvedata("AAPL", lookback_days=500, api_key="x")
    assert "API limit" in str(exc.value)


def test_fetch_alphavantage_parses_and_reports_limits(monkeypatch):
    series = {
        f"2026-02-{idx + 1:02d}": {"1. open": "5", "2. high": "6", "3. low": "4", "4. close": "5.5", "5. volume": "99"}
        for idx in range(70)
    }
    monkeypatch.setattr(market.httpx, "get", lambda *a, **k: FakeResponse({"Time Series (Daily)": series}))
    bars = market.fetch_alphavantage("MSFT", lookback_days=500, api_key="k")
    assert len(bars) == 70 and bars[-1].date == "2026-02-70"

    monkeypatch.setattr(market.httpx, "get", lambda *a, **k: FakeResponse({"Note": "превишено ліміт"}))
    with pytest.raises(ValueError) as exc:
        market.fetch_alphavantage("MSFT", lookback_days=500, api_key="k")
    assert "ліміт" in str(exc.value)


# ─── кеш і фолбек ─────────────────────────────────────────────────────────────

def test_cache_used_within_ttl(settings, monkeypatch):
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse(yahoo_payload())

    monkeypatch.setattr(market.httpx, "get", fake_get)
    monkeypatch.setattr(market, "session_is_open", lambda *a, **k: False)
    settings.config["data"]["sources"] = ["yahoo"]

    first = fetch_daily("NVDA", settings)
    second = fetch_daily("NVDA", settings)
    assert first.source == "yahoo" and "кеш" in second.source
    assert calls["n"] == 1                       # другий раз — з кеша, без мережі


def test_fallback_to_synthetic_when_all_sources_fail(settings, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("мережа недоступна")

    monkeypatch.setattr(market.httpx, "get", boom)
    settings.config["data"]["sources"] = ["yahoo", "twelvedata", "alphavantage"]
    data = fetch_daily("TSLA", settings, force_refresh=True)
    assert data.synthetic is True
    assert data.source == "synthetic"
    assert any("ДЕМО" in w for w in data.warnings)
    assert len(data.bars) == int(settings.data_cfg["lookback_days"])


def test_error_mode_raises_instead_of_synthetic(settings, monkeypatch):
    monkeypatch.setattr(market.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    settings.config["data"]["sources"] = ["yahoo"]
    settings.config["data"]["offline_fallback"] = "error"
    with pytest.raises(RuntimeError):
        fetch_daily("TSLA", settings, force_refresh=True)


def test_synthetic_bars_deterministic():
    first = synthetic_bars("NVDA", 100)
    second = synthetic_bars("NVDA", 100)
    assert [b.close for b in first] == [b.close for b in second]
    assert len(first) == 100
    assert all(b.high >= b.low and b.low > 0 for b in first)
    assert all(date.fromisoformat(b.date).weekday() < 5 for b in first)


# ─── календар сесій ───────────────────────────────────────────────────────────

def test_next_weekday_skips_weekend():
    assert next_weekday(date(2026, 10, 9)) == date(2026, 10, 12)     # пт → пн
    assert next_weekday(date(2026, 10, 12)) == date(2026, 10, 13)


def test_next_session_from_last_bar():
    bars = [Bar(date="2026-10-08", open=1, high=1, low=1, close=1, volume=1),
            Bar(date="2026-10-09", open=1, high=1, low=1, close=1, volume=1)]
    assert next_session_date(bars) == date(2026, 10, 12)


def test_market_data_helpers():
    data = MarketData(ticker="NVDA", bars=[Bar("2026-10-09", 1, 2, 0.5, 1.5, 10)], source="yahoo", fetched_at="x")
    assert data.last.close == 1.5
    assert data.to_dict()["bars"][0]["date"] == "2026-10-09"
