"""Моделі даних: сигнал від агента, рішення ризик-гейта, запис прогону."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Action = Literal["BUY", "SELL", "HOLD"]

# П'ятирівнева шкала TradingAgents → три дії, які розуміє обв'язка
RATING_TO_ACTION: dict[str, Action] = {
    "buy": "BUY",
    "strong buy": "BUY",
    "overweight": "BUY",
    "hold": "HOLD",
    "neutral": "HOLD",
    "underweight": "SELL",
    "sell": "SELL",
    "strong sell": "SELL",
}

# Якщо модель не дала числа — беремо консервативну оцінку з рівня рейтингу
RATING_TO_CONFIDENCE: dict[str, float] = {
    "buy": 0.70,
    "strong buy": 0.80,
    "overweight": 0.62,
    "hold": 0.50,
    "neutral": 0.50,
    "underweight": 0.62,
    "sell": 0.70,
    "strong sell": 0.80,
}


class Signal(BaseModel):
    """Те, що повернув агент, у нормалізованому вигляді."""

    ticker: str
    analysis_date: str
    action: Action
    rating: str | None = None            # оригінальний рейтинг моделі, якщо був
    confidence: float = 0.5
    rationale: str = ""
    reference_price: float | None = None  # ціна, на якій грунтувався аналіз
    raw: Any = None                       # сирий вихід агента — для аудиту

    @property
    def is_actionable(self) -> bool:
        return self.action in ("BUY", "SELL")


class RiskVerdict(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    quantity: int | None = None
    notional: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    kill_switch: bool = False


class RunRecord(BaseModel):
    id: int | None = None
    ticker: str
    analysis_date: str
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    engine: str = "mock"
    status: str = "pending"        # pending | ok | error
    action: str | None = None
    rating: str | None = None
    confidence: float | None = None
    rationale: str = ""
    reference_price: float | None = None
    approved: bool | None = None
    reasons: str = ""
    quantity: int | None = None
    notional: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    kill_switch: bool = False
    report_path: str | None = None
    report_html: str | None = None
    error: str | None = None
    duration_sec: float | None = None
    notified: bool = False
