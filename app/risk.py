"""Детермінований ризик-шар. Тут немає жодного LLM і жодного «судження».

Правило архітектури: LLM пропонує напрямок — цей модуль вирішує, чи взагалі
це допустимо, і який розмір позиції безпечний. Усе чистими функціями,
щоб покривалося юніт-тестами.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .models import RiskVerdict, Signal


@dataclass
class Book:
    """Стан рахунку на момент перевірки."""

    cash: float
    equity: float
    prev_close_equity: float
    positions: list[dict[str, Any]]
    currency: str = "USD"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Book":
        cash = float(data.get("cash", 0.0) or 0.0)
        positions = list(data.get("positions", []) or [])
        equity = float(data.get("equity") or (cash + sum(
            float(p.get("quantity", 0) or 0) * float(p.get("average_price", 0) or 0)
            for p in positions
        )))
        return cls(
            cash=cash,
            equity=equity,
            prev_close_equity=float(data.get("prev_close_equity") or equity),
            positions=positions,
            currency=str(data.get("currency", "USD")),
        )

    def position(self, ticker: str) -> dict[str, Any] | None:
        for p in self.positions:
            if str(p.get("ticker", "")).upper() == ticker.upper():
                return p
        return None

    def position_value(self, ticker: str, price: float | None) -> float:
        p = self.position(ticker)
        if not p:
            return 0.0
        qty = float(p.get("quantity", 0) or 0)
        px = float(price or p.get("average_price", 0) or 0)
        return qty * px

    def daily_pnl_pct(self) -> float:
        if self.prev_close_equity <= 0:
            return 0.0
        return (self.equity - self.prev_close_equity) / self.prev_close_equity * 100.0


def size_position(equity: float, price: float, max_position_pct: float) -> tuple[int, float]:
    """Цілочисельний розмір позиції. Цілими акціями — простіше агригувати з брокером."""
    if price <= 0 or equity <= 0:
        return 0, 0.0
    notional = equity * (max_position_pct / 100.0)
    qty = int(math.floor(notional / price))
    return qty, round(qty * price, 2)


def size_by_risk(equity: float, risk_pct: float, entry: float, stop: float) -> int:
    """Скільки акцій, щоб втрата на стопі дорівнювала risk_pct від капіталу."""
    risk_per_share = entry - stop
    if equity <= 0 or risk_per_share <= 0 or risk_pct <= 0:
        return 0
    budget = equity * (risk_pct / 100.0)
    return max(0, int(math.floor(budget / risk_per_share)))


def stop_loss_price(price: float, stop_loss_pct: float) -> float:
    return round(price * (1 - stop_loss_pct / 100.0), 2)


def take_profit_price(price: float, take_profit_pct: float) -> float:
    return round(price * (1 + take_profit_pct / 100.0), 2)


def _parse_day(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value[: len(fmt) + 2].strip(), fmt).date()
            except ValueError:
                continue
    return None


def check_cooldown(
    ticker: str, recent_orders: Iterable[dict[str, Any]], cooldown_days: int, today: date
) -> str | None:
    """Повертає причину відмови, якщо тікер чіпали занадто недавно."""
    if cooldown_days <= 0:
        return None
    for order in recent_orders:
        if str(order.get("ticker", "")).upper() != ticker.upper():
            continue
        day = _parse_day(order.get("created_at"))
        if day is None:
            continue
        if timedelta(days=0) <= (today - day) < timedelta(days=cooldown_days):
            return f"кулдаун: {ticker} уже мав схвалене рішення {day.isoformat()} (ліміт {cooldown_days} дн.)"
    return None


def risk_gate(
    signal: Signal,
    book: Book,
    limits: dict[str, Any],
    *,
    today: date,
    today_approved_count: int = 0,
    recent_orders: Iterable[dict[str, Any]] = (),
) -> RiskVerdict:
    """Головна перевірка. Порядок причин — від найжорсткіших до найм'якших."""
    reasons: list[str] = []

    max_position_pct = float(limits.get("max_position_pct", 2.0))
    max_positions = int(limits.get("max_positions", 8))
    daily_loss_limit = float(limits.get("daily_loss_limit_pct", 3.0))
    min_confidence = float(limits.get("min_confidence", 0.55))
    allow_short = bool(limits.get("allow_short", False))
    stop_pct = float(limits.get("stop_loss_pct", 8.0))
    tp_pct = float(limits.get("take_profit_pct", 20.0))
    cooldown_days = int(limits.get("cooldown_days", 3))
    max_orders = int(limits.get("max_orders_per_day", 3))

    # 1. Kill-switch — найвищий пріоритет, перекриває все
    daily_pnl = book.daily_pnl_pct()
    kill_switch = daily_pnl <= -abs(daily_loss_limit)
    if kill_switch:
        reasons.append(
            f"KILL-SWITCH: денний P/L {daily_pnl:+.2f}% перевищив ліміт -{daily_loss_limit:.2f}%"
        )
        return RiskVerdict(approved=False, reasons=reasons, kill_switch=True)

    # 2. Базові перевірки сигналу
    if not signal.is_actionable:
        return RiskVerdict(approved=False, reasons=["сигнал HOLD — дій не потрібно"])

    if signal.confidence < min_confidence:
        return RiskVerdict(
            approved=False,
            reasons=[f"впевненість {signal.confidence:.2f} < мінімуму {min_confidence:.2f}"],
        )

    # 3. Ліміт дій на добу
    if today_approved_count >= max_orders:
        return RiskVerdict(
            approved=False,
            reasons=[f"ліміт дій на добу вичерпано ({today_approved_count}/{max_orders})"],
        )

    # 4. Кулдаун по тікеру
    cooldown_reason = check_cooldown(signal.ticker, recent_orders, cooldown_days, today)
    if cooldown_reason:
        return RiskVerdict(approved=False, reasons=[cooldown_reason])

    # 5. Логіка напрямку
    held = book.position(signal.ticker)
    if signal.action == "BUY":
        if len(book.positions) >= max_positions and not held:
            return RiskVerdict(
                approved=False,
                reasons=[f"уже {len(book.positions)} позицій — ліміт {max_positions}"],
            )
        if held:
            reasons.append(f"позиція вже є ({held.get('quantity')} шт.) — це докупівля, не вхід")
    elif signal.action == "SELL":
        if not held and not allow_short:
            return RiskVerdict(
                approved=False,
                reasons=["SELL без наявної позиції — шорт вимкнено в налаштуваннях"],
            )
        if held and float(held.get("quantity", 0) or 0) <= 0:
            return RiskVerdict(approved=False, reasons=["немає що продавати — кількість 0"])

    # 6. Ціна й розмір позиції (тільки для BUY — для SELL закриваємо те, що є)
    reference_price = signal.reference_price
    if signal.action == "BUY":
        if not reference_price or reference_price <= 0:
            return RiskVerdict(
                approved=False,
                reasons=["немає верифікованої ціни в сигналі — розмір позиції не порахувати"],
            )
        qty, notional = size_position(book.equity, float(reference_price), max_position_pct)
        if qty <= 0:
            return RiskVerdict(
                approved=False,
                reasons=[
                    f"на {max_position_pct:.1f}% капіталу (${book.equity:,.0f}) не купити жодної акції "
                    f"по ${reference_price:,.2f} — підніміть max_position_pct або капітал"
                ],
            )
        if notional > book.cash:
            return RiskVerdict(
                approved=False,
                reasons=[f"потрібно ${notional:,.2f}, а готівки ${book.cash:,.2f}"],
            )
        return RiskVerdict(
            approved=True,
            reasons=reasons + [
                f"розмір: {qty} шт. ≈ ${notional:,.2f} ({notional / book.equity * 100:.2f}% капіталу)"
            ],
            quantity=qty,
            notional=notional,
            stop_loss_price=stop_loss_price(float(reference_price), stop_pct),
            take_profit_price=take_profit_price(float(reference_price), tp_pct),
        )

    # SELL — закриття наявної позиції
    qty_held = int(float(held.get("quantity", 0) or 0)) if held else 0
    px = float(reference_price or (held or {}).get("average_price", 0) or 0)
    return RiskVerdict(
        approved=True,
        reasons=reasons + [f"закриття позиції: {qty_held} шт. (ліміт/стоп — за вашим рішенням)"],
        quantity=qty_held,
        notional=round(qty_held * px, 2) if px else None,
    )


def explain(verdict: RiskVerdict) -> str:
    mark = "✅ ДОЗВОЛЕНО" if verdict.approved else "⛔ ВІДХИЛЕНО"
    lines = [f"{mark}"]
    for r in verdict.reasons:
        lines.append(f"  • {r}")
    if verdict.approved and verdict.quantity:
        lines.append(f"  • стоп: ${verdict.stop_loss_price} · тейк-профіт: ${verdict.take_profit_price}")
    return "\n".join(lines)
