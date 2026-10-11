"""Індикатори: перевірка на відомих числах, а не «щось порахувалось»."""

from __future__ import annotations

import pytest

from app.market import Bar
from app.technical import (
    analyze,
    atr_series,
    ema_series,
    macd_series,
    rsi_series,
    sma_series,
    series_for_chart,
)


def make_bars(closes: list[float], *, high_extra: float = 1.0, low_extra: float = 1.0) -> list[Bar]:
    bars = []
    for idx, close in enumerate(closes):
        bars.append(
            Bar(
                date=f"2026-01-{idx + 1:02d}" if idx < 31 else f"2026-02-{idx - 30:02d}",
                open=close,
                high=close + high_extra,
                low=close - low_extra,
                close=close,
                volume=1_000_000 + idx,
            )
        )
    return bars


# ─── SMA / EMA ────────────────────────────────────────────────────────────────

def test_sma_simple():
    series = sma_series([1, 2, 3, 4, 5], 3)
    assert series[:2] == [None, None]
    assert series[2] == pytest.approx(2.0)
    assert series[4] == pytest.approx(4.0)


def test_sma_period_longer_than_series():
    assert sma_series([1, 2], 5) == [None, None]


def test_ema_seed_and_step():
    values = [1.0] * 10
    series = ema_series(values, 3)
    assert series[2] == pytest.approx(1.0)
    assert series[-1] == pytest.approx(1.0)          # константа залишається собою
    rising = ema_series([float(i) for i in range(1, 11)], 3)
    assert rising[-1] > rising[-2] > 0               # реагує на напрямок


# ─── RSI ──────────────────────────────────────────────────────────────────────

def test_rsi_all_gains_is_100():
    series = rsi_series([float(i) for i in range(1, 20)], 14)
    assert series[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_near_zero():
    series = rsi_series([float(i) for i in range(20, 1, -1)], 14)
    assert series[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_mixed_is_between_bounds():
    values = [10, 11, 10.5, 12, 11.5, 13, 12, 14, 13.5, 15, 14, 16, 15.5, 17, 16, 18, 17, 19, 18, 20]
    series = rsi_series([float(v) for v in values], 14)
    assert 0 < series[-1] < 100


# ─── MACD / ATR ───────────────────────────────────────────────────────────────

def test_macd_zero_on_flat_series():
    macd, signal, hist = macd_series([100.0] * 60)
    assert macd[-1] == pytest.approx(0.0)
    assert signal[-1] == pytest.approx(0.0)
    assert hist[-1] == pytest.approx(0.0)


def test_macd_positive_on_uptrend():
    macd, signal, hist = macd_series([float(i) for i in range(1, 80)])
    assert macd[-1] > 0 and hist[-1] is not None


def test_atr_uses_true_range_gaps():
    bars = make_bars([100, 101, 102, 103, 104] * 5)
    series = atr_series(bars, 14)
    assert series[-1] is not None and series[-1] > 0


# ─── analyze ──────────────────────────────────────────────────────────────────

def test_analyze_uptrend_detected():
    bars = make_bars([100 + i * 1.0 for i in range(120)])
    tech = analyze("TEST", bars)
    assert tech.trend == "up"
    assert tech.last_close > tech.sma50 > 0
    assert tech.rsi14 is not None and tech.rsi14 > 50
    assert tech.high10 >= tech.high20 * 0.95      # короткий максимум близько до довгого
    assert tech.low10 >= tech.low20
    assert tech.atr_pct > 0
    assert isinstance(tech.signals, list)


def test_analyze_downtrend_detected():
    bars = make_bars([200 - i * 1.0 for i in range(120)])
    tech = analyze("TEST", bars)
    assert tech.trend == "down"
    assert tech.last_close < tech.sma50


def test_analyze_requires_enough_bars():
    with pytest.raises(ValueError):
        analyze("TEST", make_bars([100] * 10))


def test_analyze_to_dict_is_json_ready():
    import json

    tech = analyze("TEST", make_bars([100 + i * 0.5 for i in range(120)]))
    json.dumps(tech.to_dict())                    # не має кидати
    summary = tech.summary_uk()
    assert "RSI" in summary and "тренд" in summary


# ─── дані для графіка ─────────────────────────────────────────────────────────

def test_series_for_chart_lengths_match():
    bars = make_bars([100 + i for i in range(120)])
    data = series_for_chart(bars, last_n=90)
    assert len(data["bars"]) == 90
    assert len(data["sma20"]) == 90 and len(data["sma50"]) == 90
    assert data["sma50"][-1] is not None
