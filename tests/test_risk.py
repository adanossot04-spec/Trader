"""Ризик-шар — найважливіше покриття: тут вирішується, що взагалі дозволено."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import Signal
from app.risk import Book, check_cooldown, risk_gate, size_position, stop_loss_price, take_profit_price

TODAY = date(2026, 10, 11)


def make_signal(action="BUY", confidence=0.7, price=100.0, ticker="NVDA") -> Signal:
    return Signal(
        ticker=ticker,
        analysis_date=TODAY.isoformat(),
        action=action,  # type: ignore[arg-type]
        rating="Buy" if action == "BUY" else action,
        confidence=confidence,
        rationale="тест",
        reference_price=price,
    )


def limits(**over) -> dict:
    base = {
        "max_position_pct": 2.0,
        "max_positions": 8,
        "daily_loss_limit_pct": 3.0,
        "min_confidence": 0.55,
        "allow_short": False,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 20.0,
        "cooldown_days": 3,
        "max_orders_per_day": 3,
    }
    base.update(over)
    return base


# ─── базові розрахунки ────────────────────────────────────────────────────────

def test_position_size_rounds_down_to_whole_shares():
    qty, notional = size_position(equity=10000, price=182.40, max_position_pct=2.0)
    assert qty == 1                      # 200 / 182.40 = 1.09 → 1
    assert notional == pytest.approx(182.40)


def test_position_size_zero_when_not_affordable():
    qty, notional = size_position(equity=100, price=500, max_position_pct=2.0)
    assert qty == 0 and notional == 0.0


def test_stop_and_take_levels():
    assert stop_loss_price(100.0, 8.0) == 92.0
    assert take_profit_price(100.0, 20.0) == 120.0


# ─── дозволений сценарій ──────────────────────────────────────────────────────

def test_buy_approved_within_lmits(book_dict):
    book = Book.from_dict(book_dict)
    verdict = risk_gate(make_signal(), book, limits(), today=TODAY)
    assert verdict.approved
    assert verdict.quantity == 2          # 2% від $10 000 / $100 = 2 акції
    assert verdict.stop_loss_price == pytest.approx(92.0)
    assert verdict.take_profit_price == pytest.approx(120.0)


# ─── жорсткі заборони ────────────────────────────────────────────────────────

def test_kill_switch_blocks_everything(book_dict):
    book_dict["prev_close_equity"] = 10000.0
    book_dict["cash"] = 9500.0
    book = Book.from_dict(book_dict)
    book.equity = 9500.0                          # −5% за день
    verdict = risk_gate(make_signal(), book, limits(daily_loss_limit_pct=3.0), today=TODAY)
    assert not verdict.approved
    assert verdict.kill_switch
    assert "KILL-SWITCH" in verdict.reasons[0]


def test_hold_is_not_actionable(book_dict):
    verdict = risk_gate(make_signal(action="HOLD"), Book.from_dict(book_dict), limits(), today=TODAY)
    assert not verdict.approved
    assert "HOLD" in verdict.reasons[0]


def test_low_confidence_rejected(book_dict):
    verdict = risk_gate(make_signal(confidence=0.4), Book.from_dict(book_dict), limits(), today=TODAY)
    assert not verdict.approved
    assert "впевненість" in verdict.reasons[0]


def test_daily_order_limit(book_dict):
    verdict = risk_gate(
        make_signal(), Book.from_dict(book_dict), limits(max_orders_per_day=2), today=TODAY,
        today_approved_count=2,
    )
    assert not verdict.approved
    assert "ліміт дій на добу" in verdict.reasons[0]


def test_short_disabled_rejects_sell_without_position(book_dict):
    verdict = risk_gate(make_signal(action="SELL"), Book.from_dict(book_dict), limits(), today=TODAY)
    assert not verdict.approved
    assert "шорт" in verdict.reasons[0]


def test_short_allowed_permits_sell(book_dict):
    verdict = risk_gate(
        make_signal(action="SELL"), Book.from_dict(book_dict), limits(allow_short=True), today=TODAY
    )
    assert verdict.approved


def test_max_positions_limit(book_dict):
    book_dict["positions"] = [
        {"ticker": f"T{i}", "quantity": 1, "average_price": 100.0} for i in range(8)
    ]
    verdict = risk_gate(make_signal(ticker="AMD"), Book.from_dict(book_dict), limits(), today=TODAY)
    assert not verdict.approved
    assert "ліміт 8" in verdict.reasons[0]


def test_insufficient_cash(book_dict):
    book_dict["cash"] = 50.0                 # equity явно, щоб не спрацював kill-switch
    book_dict["equity"] = 10000.0
    verdict = risk_gate(make_signal(price=100.0), Book.from_dict(book_dict), limits(), today=TODAY)
    assert not verdict.approved
    assert "готівки" in verdict.reasons[0]


def test_price_too_high_for_budget(book_dict):
    verdict = risk_gate(make_signal(price=5000.0), Book.from_dict(book_dict), limits(), today=TODAY)
    assert not verdict.approved
    assert "жодної акції" in verdict.reasons[0]


def test_missing_price_rejected_for_buy(book_dict):
    signal = make_signal()
    signal.reference_price = None
    verdict = risk_gate(signal, Book.from_dict(book_dict), limits(), today=TODAY)
    assert not verdict.approved
    assert "верифікованої ціни" in verdict.reasons[0]


# ─── кулдаун ─────────────────────────────────────────────────────────────────

def test_cooldown_blocks_recent_ticker():
    recent = [{"ticker": "NVDA", "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}]
    reason = check_cooldown("NVDA", recent, cooldown_days=3, today=datetime.now(timezone.utc).date())
    assert reason and "кулдаун" in reason


def test_cooldown_allows_after_window():
    recent = [{"ticker": "NVDA", "created_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()}]
    assert check_cooldown("NVDA", recent, cooldown_days=3, today=datetime.now(timezone.utc).date()) is None


def test_cooldown_ignores_other_tickers():
    recent = [{"ticker": "AAPL", "created_at": datetime.now(timezone.utc).isoformat()}]
    assert check_cooldown("NVDA", recent, cooldown_days=3, today=datetime.now(timezone.utc).date()) is None


def test_cooldown_disabled_when_zero():
    recent = [{"ticker": "NVDA", "created_at": datetime.now(timezone.utc).isoformat()}]
    assert check_cooldown("NVDA", recent, cooldown_days=0, today=datetime.now(timezone.utc).date()) is None


# ─── продаж наявної позиції ───────────────────────────────────────────────────

def test_sell_closes_existing_position(book_dict):
    book_dict["positions"] = [{"ticker": "NVDA", "quantity": 5, "average_price": 150.0}]
    verdict = risk_gate(make_signal(action="SELL", price=180.0), Book.from_dict(book_dict), limits(), today=TODAY)
    assert verdict.approved
    assert verdict.quantity == 5


def test_buy_when_position_exists_is_flagged_as_addon(book_dict):
    book_dict["positions"] = [{"ticker": "NVDA", "quantity": 3, "average_price": 100.0}]
    verdict = risk_gate(make_signal(), Book.from_dict(book_dict), limits(), today=TODAY)
    assert verdict.approved
    assert any("докупівля" in r for r in verdict.reasons)


# ─── денний P/L ───────────────────────────────────────────────────────────────

def test_daily_pnl_pct_from_prev_close(book_dict):
    book = Book.from_dict(book_dict)
    book.equity = 10200.0
    assert book.daily_pnl_pct() == pytest.approx(2.0)
