"""Пропозиція на наступну сесію: правила, рівні, розмір, вердикт гейта."""

from __future__ import annotations

import pytest

from app import proposal as proposal_module
from app.db import Store
from app.market import Bar
from app.risk import Book, size_by_risk
from app.technical import analyze


def bars_from(closes: list[float], *, spread: float = 1.0) -> list[Bar]:
    import datetime as dt

    bars, day = [], dt.date(2025, 1, 1)
    for close in closes:
        bars.append(
            Bar(date=day.isoformat(), open=close, high=close + spread, low=close - spread, close=close, volume=1e6)
        )
        day += dt.timedelta(days=1)
    return bars


def realistic_uptrend(seed: int = 10, n: int = 200) -> list[Bar]:
    """Детермінований «реалістичний» висхідний ряд: дрейф угору + відкати.

    Чистий монотонний ріст дає RSI=100 (перекупленість) — це не сетап, тому й потрібен
    ряд із шумом, який дає trend=up і RSI у робочому діапазоні.
    """
    import datetime as _dt
    import random

    rng = random.Random(seed)
    bars, day, price = [], _dt.date(2025, 1, 1), 100.0
    for _ in range(n):
        price = max(5.0, price * (1 + rng.gauss(0.0022, 0.011)))
        bars.append(
            Bar(date=day.isoformat(), open=round(price * 0.998, 2), high=round(price * 1.006, 2),
                low=round(price * 0.994, 2), close=round(price, 2), volume=1_000_000)
        )
        day += _dt.timedelta(days=1)
    return bars


def uptrend(n: int = 200) -> list[Bar]:
    return realistic_uptrend(n=n)


def downtrend(n: int = 140) -> list[Bar]:
    return bars_from([300 - idx * 0.8 for idx in range(n)])


# ─── розмір від ризику ────────────────────────────────────────────────────────

def test_size_by_risk_basic():
    # equity 100 000, ризик 0.5% = $500, ризик/акція $5 → 100 акцій
    assert size_by_risk(100_000, 0.5, entry=100.0, stop=95.0) == 100


def test_size_by_risk_zero_cases():
    assert size_by_risk(0, 0.5, 100, 95) == 0
    assert size_by_risk(100_000, 0.5, 100, 100) == 0     # немає ризику — немає угоди
    assert size_by_risk(100_000, 0, 100, 95) == 0


# ─── план: бичачий сценарій ───────────────────────────────────────────────────

def test_plan_bullish_has_all_levels(settings):
    tech = analyze("TEST", realistic_uptrend())
    plan = proposal_module.plan_from_technical(tech, settings, has_position=False)

    assert plan["bias"] == "BUY"
    assert plan["entry"] <= tech.last_close                     # ніколи не вище закриття
    assert plan["stop"] < plan["entry"] < plan["target"]
    assert plan["rr"] >= float(settings.proposal_cfg["min_rr"])
    risk_pct = (plan["entry"] - plan["stop"]) / plan["entry"] * 100
    assert risk_pct <= float(settings.proposal_cfg["max_stop_pct"]) + 1e-9
    assert plan["confidence"] > 0.5


def test_plan_stop_sits_below_recent_structure(settings):
    bars = realistic_uptrend()
    tech = analyze("TEST", bars)
    plan = proposal_module.plan_from_technical(tech, settings, has_position=False)
    # стоп має бути або під мінімумом 10 сесій, або на рівно ATR-відстані
    atr_distance = plan["entry"] - plan["stop"]
    assert plan["stop"] <= tech.low10 * 0.995 + 1e-6 or atr_distance == pytest.approx(
        float(settings.proposal_cfg["atr_stop_mult"]) * tech.atr14, rel=0.02
    )


def test_plan_needs_bull_structure(settings):
    tech = analyze("TEST", downtrend())
    plan = proposal_module.plan_from_technical(tech, settings, has_position=False)
    assert plan["bias"] == "WAIT"
    assert plan["reasons"]


def test_plan_rejects_extreme_volatility(settings):
    settings.config["proposal"]["max_atr_pct"] = 0.2      # неможливий поріг
    tech = analyze("TEST", realistic_uptrend())
    plan = proposal_module.plan_from_technical(tech, settings, has_position=False)
    assert plan["bias"] == "WAIT"
    assert "волатильн" in " ".join(plan["reasons"]).lower()


def test_plan_waits_for_too_deep_pullback(settings):
    """Якщо структура вимагає входу далеко під ціною — краще пропустити."""
    settings.config["proposal"]["max_pullback_atr"] = 0.05     # майже забороняємо відкат
    settings.config["proposal"]["entry_pullback_atr"] = 3.0
    tech = analyze("TEST", realistic_uptrend())
    plan = proposal_module.plan_from_technical(tech, settings, has_position=False)
    assert plan["bias"] == "WAIT"
    assert "відкат" in " ".join(plan["reasons"]).lower()


# ─── повна збірка пропозиції ─────────────────────────────────────────────────

def test_build_proposal_end_to_end(settings):
    store = Store(settings.db_path)
    settings.config["data"]["sources"] = ["synthetic"]

    proposal = proposal_module.build_proposal(settings, store, "NVDA", use_llm=False)

    assert proposal.ticker == "NVDA"
    assert proposal.for_session and proposal.data_source == "synthetic"
    assert proposal.synthetic is True
    assert proposal.note and proposal.note_source == "rules"
    assert proposal.id is not None
    assert store.get_proposal(proposal.id) is not None

    if proposal.bias == "BUY":
        assert proposal.entry_price and proposal.stop_price and proposal.target_price
        if proposal.approved:
            assert proposal.qty >= 0 and proposal.order_template


def test_build_proposal_respects_daily_order_limit(settings):
    """Ліміт дій на добу має зупиняти зайві схвалені пропозиції."""
    from app import pipeline

    store = Store(settings.db_path)
    settings.risk["max_orders_per_day"] = 1

    approved, blocked = 0, 0
    for ticker in ["NVDA", "AAPL", "MSFT", "SPY", "TSLA"]:
        proposal = pipeline.propose_one(settings, store, ticker, notify=False, use_llm=False)
        if proposal.approved:
            approved += 1
        else:
            blocked += 1
            assert proposal.gate_reasons, "відмова мусить мати причину"
    assert approved <= 1
    assert approved + blocked == 5


def test_replan_same_ticker_session_is_not_blocked(settings):
    """Прогін за годину до відкриття переписує вчорашній план, а не блокується ним."""
    from app import pipeline

    store = Store(settings.db_path)
    settings.risk["max_orders_per_day"] = 1
    # синтетичні ціни ~$500 — потрібен капітал, щоб 2%-ліміт дозволяв хоч одну акцію
    settings.config["portfolio"] = {"currency": "USD", "cash": 50_000.0, "prev_close_equity": 50_000.0, "positions": []}
    settings.risk["max_position_pct"] = 5.0

    first = pipeline.propose_one(settings, store, "NVDA", notify=False, use_llm=False)
    assert first.approved, first.gate_reasons               # перший план схвалено
    again = pipeline.propose_one(settings, store, "NVDA", notify=False, use_llm=False)
    assert again.approved, again.gate_reasons               # повторний прогін не блокується кулдауном/лімітом

    other = pipeline.propose_one(settings, store, "MSFT", notify=False, use_llm=False)
    if other.bias == "BUY":
        assert not other.approved                            # а інший тікер упирається в ліміт 1 дії на добу
        assert any("ліміт дій" in reason for reason in other.gate_reasons)


def test_wait_proposal_has_no_gate_noise(settings):
    store = Store(settings.db_path)
    proposal = proposal_module.build_proposal(settings, store, "SPY", use_llm=False)
    if proposal.bias == "WAIT":
        assert "немає сетапу — ризик-гейт не застосовується" in proposal.gate_reasons
        assert proposal.qty == 0


def test_order_template_mentions_levels(settings):
    store = Store(settings.db_path)
    proposal = proposal_module.build_proposal(settings, store, "MSFT", use_llm=False)
    if proposal.bias == "BUY" and proposal.qty:
        template = proposal.order_template
        assert proposal.ticker in template
        assert f"{proposal.stop_price:.2f}" in template
        assert f"{proposal.target_price:.2f}" in template


def test_proposal_survives_unavailable_llm(settings):
    """Збій LLM не ламає пропозицію: цифри залишаються, коментар підставляє код."""
    import app.llm as llm_module
    import app.proposal as proposal_mod

    settings.config["llm"]["provider"] = "groq"
    original = llm_module.narrate
    llm_module.narrate = lambda payload, st: (None, "RuntimeError: тестовий збій")
    try:
        store = Store(settings.db_path)
        proposal = proposal_mod.build_proposal(settings, store, "AAPL", use_llm=True)
    finally:
        llm_module.narrate = original

    assert proposal.note and proposal.note_source == "rules"
    assert proposal.llm_error and "тестовий збій" in proposal.llm_error
