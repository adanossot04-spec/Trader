"""Дані для прогону за годину до відкриття: незакриті сесії, час відкриття, ґеп-перевірка."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app import market, proposal as proposal_module
from app.market import Bar, completed_sessions_only, minutes_to_open, next_open_datetime
from app.risk import Book

ET = ZoneInfo("America/New_York")


def bar(day: str, close: float = 100.0) -> Bar:
    return Bar(date=day, open=close, high=close + 1, low=close - 1, close=close, volume=1e6)


def series(days: list[str]) -> list[Bar]:
    return [bar(day, 100 + idx) for idx, day in enumerate(days)]


# ─── незакриті сесії не потрапляють у план ───────────────────────────────────

def test_premarket_drops_today_bar():
    """8:30 у понеділок: свічка понеділка ще не сформована — її не використовуємо."""
    moment = dt.datetime(2026, 10, 12, 8, 30, tzinfo=ET)
    bars = series(["2026-10-08", "2026-10-09", "2026-10-12"])
    kept = completed_sessions_only(bars, moment)
    assert [b.date for b in kept] == ["2026-10-08", "2026-10-09"]


def test_after_close_keeps_today_bar():
    moment = dt.datetime(2026, 10, 12, 16, 15, tzinfo=ET)
    bars = series(["2026-10-09", "2026-10-12"])
    kept = completed_sessions_only(bars, moment)
    assert kept[-1].date == "2026-10-12"


def test_intraday_drops_today_bar():
    moment = dt.datetime(2026, 10, 12, 13, 0, tzinfo=ET)
    bars = series(["2026-10-09", "2026-10-12"])
    assert completed_sessions_only(bars, moment)[-1].date == "2026-10-09"


def test_weekend_keeps_last_friday_bar():
    moment = dt.datetime(2026, 10, 11, 12, 0, tzinfo=ET)      # неділя
    bars = series(["2026-10-08", "2026-10-09"])
    assert completed_sessions_only(bars, moment)[-1].date == "2026-10-09"


def test_target_session_is_upcoming_one():
    """Після відсіву сьогоднішньої свічки «наступна сесія» = сьогодні (ми ж до відкриття)."""
    moment = dt.datetime(2026, 10, 12, 8, 30, tzinfo=ET)
    bars = completed_sessions_only(series(["2026-10-08", "2026-10-09", "2026-10-12"]), moment)
    assert market.next_session_date(bars).isoformat() == "2026-10-12"


def test_friday_evening_targets_monday():
    moment = dt.datetime(2026, 10, 9, 17, 0, tzinfo=ET)
    bars = series(["2026-10-08", "2026-10-09"])
    assert market.next_session_date(completed_sessions_only(bars, moment)).isoformat() == "2026-10-12"


# ─── час відкриття ───────────────────────────────────────────────────────────

def test_minutes_to_open_before_open():
    moment = dt.datetime(2026, 10, 12, 8, 30, tzinfo=ET)
    assert minutes_to_open(moment) == 60
    assert next_open_datetime(moment) == dt.datetime(2026, 10, 12, 9, 30, tzinfo=ET)


def test_minutes_to_open_during_session_is_next_day():
    moment = dt.datetime(2026, 10, 12, 13, 0, tzinfo=ET)
    assert next_open_datetime(moment).date() == dt.date(2026, 10, 13)


def test_minutes_to_open_on_weekend():
    moment = dt.datetime(2026, 10, 11, 12, 0, tzinfo=ET)      # неділя
    assert next_open_datetime(moment).date() == dt.date(2026, 10, 12)
    assert minutes_to_open(moment) == int((dt.datetime(2026, 10, 12, 9, 30, tzinfo=ET) - moment).total_seconds() // 60)


# ─── премаркет-ціна ──────────────────────────────────────────────────────────

def test_premarket_quote_parses_minute_payload(monkeypatch):
    from app import market as market_module

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            # 04:00–09:29 ET у понеділок + кілька регулярних барів
            stamps, closes = [], []
            base = dt.datetime(2026, 10, 12, 4, 0, tzinfo=ET)
            for idx in range(8):
                moment = base + dt.timedelta(minutes=idx * 60)      # 04:00 … 11:00
                stamps.append(int(moment.timestamp()))
                closes.append(100 + idx)
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {"chartPreviousClose": 99.5, "regularMarketPrice": 107.0},
                            "timestamp": stamps,
                            "indicators": {"quote": [{"close": closes}]},
                        }
                    ]
                }
            }

    monkeypatch.setattr(market_module.httpx, "get", lambda *a, **k: Response())
    quote = market_module.premarket_quote("NVDA")
    assert quote is not None
    assert quote["price"] == 105.0                    # 08:00 ET — останній премаркет-бар
    assert quote["session_date"] == "2026-10-12"
    assert quote["previous_close"] == 99.5
    assert quote["source"] == "yahoo-1m-premarket"


def test_premarket_quote_returns_none_without_premarket(monkeypatch):
    from app import market as market_module

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            moment = dt.datetime(2026, 10, 12, 12, 0, tzinfo=ET)     # лише регулярна сесія
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {"chartPreviousClose": 99.5},
                            "timestamp": [int(moment.timestamp())],
                            "indicators": {"quote": [{"close": [101.0]}]},
                        }
                    ]
                }
            }

    monkeypatch.setattr(market_module.httpx, "get", lambda *a, **k: Response())
    assert market_module.premarket_quote("NVDA") is None


def test_premarket_quote_survives_network_error(monkeypatch):
    from app import market as market_module

    monkeypatch.setattr(
        market_module.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("немає мережі"))
    )
    assert market_module.premarket_quote("NVDA") is None


# ─── ґеп-логіка в пропозиції ─────────────────────────────────────────────────

def make_proposal(**over):
    base = dict(
        ticker="NVDA", for_session="2026-10-12", last_bar_date="2026-10-09",
        last_close=100.0, change_pct=0.0, bias="BUY", entry_type="limit",
        entry_price=99.0, stop_price=95.0, target_price=110.0, rr=2.2,
        qty=10, notional=990.0, approved=True, gate_reasons=[],
    )
    base.update(over)
    return proposal_module.Proposal(**base)


def quote(price: float, session: str = "2026-10-12") -> dict:
    return {"price": price, "as_of": f"{session}T08:30-04:00", "session_date": session, "previous_close": 100.0}


def test_premarket_gap_within_limits_keeps_plan(settings):
    proposal = proposal_module.apply_premarket(make_proposal(), settings, quote=quote(100.5))
    assert proposal.approved and proposal.qty == 10
    assert proposal.premarket_gap_pct == pytest.approx(0.5)
    assert "у межах" in proposal.premarket_note


def test_premarket_below_stop_cancels_trade(settings):
    proposal = proposal_module.apply_premarket(make_proposal(), settings, quote=quote(94.0))
    assert not proposal.approved and proposal.qty == 0 and proposal.notional is None
    assert "нижче стопу" in proposal.premarket_note
    assert any("нижче стопу" in reason for reason in proposal.gate_reasons)


def test_premarket_deep_gap_down_cancels_trade(settings):
    proposal = proposal_module.apply_premarket(make_proposal(), settings, quote=quote(97.0))
    assert not proposal.approved and "Ґеп униз" in proposal.premarket_note


def test_premarket_gap_up_warns_about_limit_order(settings):
    proposal = proposal_module.apply_premarket(make_proposal(), settings, quote=quote(102.0))
    assert proposal.approved                       # план лишається, але з попередженням
    assert "не виконається" in proposal.premarket_note
    assert any("ґеп угору" in reason.lower() or "Ґеп угору" in reason for reason in proposal.gate_reasons)


def test_premarket_skipped_for_other_session(settings):
    proposal = proposal_module.apply_premarket(make_proposal(), settings, quote=quote(90.0, session="2026-10-09"))
    assert proposal.approved and proposal.premarket_price is None


def test_premarket_not_fetched_without_yahoo_source(settings, monkeypatch):
    """Якщо Yahoo не в джерелах — не робимо зайвого запиту (і тести лишаються офлайн)."""
    settings.config["data"]["sources"] = ["synthetic"]
    calls: list[str] = []
    monkeypatch.setattr(
        proposal_module.market_module,
        "premarket_quote",
        lambda ticker, **kw: calls.append(ticker) or quote(100.0),
    )
    proposal = proposal_module.apply_premarket(make_proposal(), settings)
    assert calls == [] and proposal.premarket_price is None and proposal.approved


def test_premarket_fetched_when_yahoo_listed(settings, monkeypatch):
    settings.config["data"]["sources"] = ["yahoo", "twelvedata"]
    monkeypatch.setattr(
        proposal_module.market_module, "premarket_quote", lambda ticker, **kw: quote(100.2)
    )
    proposal = proposal_module.apply_premarket(make_proposal(), settings)
    assert proposal.premarket_price == 100.2


def test_premarket_not_applied_for_wait(settings):
    proposal = proposal_module.apply_premarket(make_proposal(bias="WAIT"), settings, quote=quote(90.0))
    assert proposal.premarket_price is None


def test_order_template_includes_premarket_note(settings):
    proposal = proposal_module.apply_premarket(make_proposal(), settings, quote=quote(102.0))
    proposal.order_template = proposal_module.build_order_template(proposal)
    assert "Ґеп угору" in proposal.order_template


# ─── повний прогін «за годину до відкриття» ──────────────────────────────────

def test_build_proposal_with_premarket(settings, monkeypatch):
    """Повний прогін «за годину до відкриття»: дані до 09.10, премаркет на 12.10."""
    import random

    from app.db import Store
    from app.market import MarketData
    from app import market as market_module

    # висхідний ряд із відкатами, який дає сетап BUY (той самий підхід, що в test_proposal)
    rng = random.Random(11)                       # seed підібрано так, щоб план був BUY
    bars, day, price = [], dt.date(2026, 1, 1), 100.0
    while day <= dt.date(2026, 10, 9):
        if day.weekday() < 5:
            price = max(5.0, price * (1 + rng.gauss(0.0022, 0.011)))
            bars.append(Bar(date=day.isoformat(), open=round(price * 0.998, 2), high=round(price * 1.006, 2),
                            low=round(price * 0.994, 2), close=round(price, 2), volume=1_000_000))
        day += dt.timedelta(days=1)
    assert bars[-1].date == "2026-10-09"

    data = MarketData(ticker="NVDA", bars=bars, source="yahoo", fetched_at="2026-10-11T12:00:00+00:00")
    monkeypatch.setattr(proposal_module.market_module, "fetch_daily", lambda *a, **k: data)
    monkeypatch.setattr(
        proposal_module.market_module,
        "premarket_quote",
        lambda ticker, **kw: {
            "price": round(bars[-1].close * 1.004, 2),
            "as_of": "2026-10-12T08:30-04:00",
            "session_date": "2026-10-12",
            "previous_close": bars[-1].close,
            "source": "test",
        },
    )

    settings.config["data"]["sources"] = ["yahoo"]      # премаркет-шлях увімкнений
    # у тестових дефолтах капітал $10 000 і ліміт позиції 2% — на акцію за $210 цього не вистачить
    settings.config["portfolio"] = {"currency": "USD", "cash": 50_000.0, "prev_close_equity": 50_000.0, "positions": []}
    store = Store(settings.db_path)
    proposal = proposal_module.build_proposal(settings, store, "NVDA", use_llm=False)

    assert proposal.for_session == "2026-10-12"
    assert proposal.bias == "BUY", proposal.reasons
    assert proposal.premarket_price is not None
    assert proposal.premarket_gap_pct == pytest.approx(0.4, abs=0.05)
    assert "у межах" in proposal.premarket_note
    assert proposal.approved
