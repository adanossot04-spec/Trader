"""Технічні індикатори по добових свічках — чиста математика, без LLM і без залежностей.

Кожна функція — окремо тестована. Результат `analyze()` — повний «паспорт» добового графіка,
на якому будується пропозиція на наступну сесію.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from .market import Bar

Number = float | None


# ─── базові індикатори ────────────────────────────────────────────────────────

def sma_series(values: Sequence[float], period: int) -> list[Number]:
    out: list[Number] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for idx, value in enumerate(values):
        running += value
        if idx >= period:
            running -= values[idx - period]
        if idx >= period - 1:
            out[idx] = running / period
    return out


def ema_series(values: Sequence[float], period: int) -> list[Number]:
    out: list[Number] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    multiplier = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for idx in range(period, len(values)):
        prev = (values[idx] - prev) * multiplier + prev
        out[idx] = prev
    return out


def rsi_series(values: Sequence[float], period: int = 14) -> list[Number]:
    """RSI за Вайлдером (згладжування як в оригіналі, не просте середнє)."""
    out: list[Number] = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = 0.0, 0.0
    for idx in range(1, period + 1):
        delta = values[idx] - values[idx - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for idx in range(period + 1, len(values)):
        delta = values[idx] - values[idx - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[idx] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def macd_series(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Number], list[Number], list[Number]]:
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    macd: list[Number] = [
        (f - s) if (f is not None and s is not None) else None for f, s in zip(ema_fast, ema_slow)
    ]
    clean = [v for v in macd if v is not None]
    signal_clean = ema_series(clean, signal)
    signal_full: list[Number] = [None] * len(macd)
    offset = len(macd) - len(clean)
    for idx, value in enumerate(signal_clean):
        if value is not None:
            signal_full[offset + idx] = value
    hist: list[Number] = [
        (m - s) if (m is not None and s is not None) else None for m, s in zip(macd, signal_full)
    ]
    return macd, signal_full, hist


def true_range_series(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    for idx, bar in enumerate(bars):
        if idx == 0:
            out.append(bar.high - bar.low)
            continue
        prev_close = bars[idx - 1].close
        out.append(max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)))
    return out


def atr_series(bars: Sequence[Bar], period: int = 14) -> list[Number]:
    trs = true_range_series(bars)
    out: list[Number] = [None] * len(bars)
    if len(bars) <= period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for idx in range(period, len(bars)):
        prev = (prev * (period - 1) + trs[idx]) / period
        out[idx] = prev
    return out


def _last(series: Sequence[Number]) -> Number:
    for value in reversed(series):
        if value is not None:
            return value
    return None


def _pct_change(new: float, old: float) -> float:
    return 0.0 if not old else (new - old) / old * 100.0


# ─── підсумковий аналіз ───────────────────────────────────────────────────────

@dataclass
class Technical:
    ticker: str
    last_date: str
    last_close: float
    prev_close: float
    change_pct: float
    sma20: Number
    sma50: Number
    sma200: Number
    ema21: Number
    rsi14: Number
    macd: Number
    macd_signal: Number
    macd_hist: Number
    macd_hist_prev: Number
    atr14: Number
    atr_pct: Number
    high10: float
    low10: float
    high20: float
    low20: float
    high52: float
    low52: float
    dist_high20_pct: float
    dist_low20_pct: float
    avg_vol20: float
    vol_ratio: float
    trend: str            # up | down | range
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    def summary_uk(self) -> str:
        return (
            f"{self.ticker}: закриття {self.last_close:.2f} ({self.change_pct:+.2f}%) · тренд {self.trend} · "
            f"RSI {self.rsi14:.1f} · ATR {self.atr_pct:.2f}% · до max20 {self.dist_high20_pct:+.2f}%"
        )


def analyze(ticker: str, bars: Sequence[Bar]) -> Technical:
    if len(bars) < 60:
        raise ValueError(f"{ticker}: замало свічок для аналізу ({len(bars)})")

    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    sma20 = _last(sma_series(closes, 20))
    sma50 = _last(sma_series(closes, 50))
    sma200 = _last(sma_series(closes, 200)) if len(closes) >= 200 else None
    ema21 = _last(ema_series(closes, 21))
    rsi14 = _last(rsi_series(closes, 14))
    macd_line, macd_signal, macd_hist = macd_series(closes)
    atr14 = _last(atr_series(bars, 14))

    last_close = closes[-1]
    prev_close = closes[-2]
    window10 = bars[-10:]
    window20 = bars[-20:]
    window52 = bars[-252:]
    high10 = max(b.high for b in window10)
    low10 = min(b.low for b in window10)
    high20 = max(b.high for b in window20)
    low20 = min(b.low for b in window20)
    high52 = max(b.high for b in window52)
    low52 = min(b.low for b in window52)
    avg_vol20 = sum(b.volume for b in window20) / len(window20)
    vol_ratio = (volumes[-1] / avg_vol20) if avg_vol20 else 1.0

    trend = "range"
    if sma20 and sma50:
        if last_close > sma50 and sma20 > sma50:
            trend = "up"
        elif last_close < sma50 and sma20 < sma50:
            trend = "down"

    signals: list[str] = []
    if sma20 and sma50:
        signals.append(
            "SMA20 вище SMA50 (бичача структура)" if sma20 > sma50 else "SMA20 нижче SMA50 (ведмежа структура)"
        )
    if sma200:
        signals.append("ціна над SMA200" if last_close > sma200 else "ціна під SMA200")
    if rsi14 is not None:
        if rsi14 >= 70:
            signals.append("RSI у зоні перекупленості (≥70)")
        elif rsi14 <= 30:
            signals.append("RSI у зоні перепроданості (≤30)")
    if macd_hist[-1] is not None and macd_hist[-2] is not None:
        prev_hist = macd_hist[-2]
        if macd_hist[-1] > 0 and prev_hist <= 0:
            signals.append("MACD перетнув сигнальну вгору")
        elif macd_hist[-1] < 0 and prev_hist >= 0:
            signals.append("MACD перетнув сигнальну вниз")
    if last_close >= high20 * 0.995:
        signals.append("ціна біля максимуму 20 сесій — потенційний пробій")
    if vol_ratio >= 1.5:
        signals.append(f"обсяг ×{vol_ratio:.2f} до середнього за 20 днів")

    return Technical(
        ticker=ticker.upper(),
        last_date=bars[-1].date,
        last_close=round(last_close, 4),
        prev_close=round(prev_close, 4),
        change_pct=round(_pct_change(last_close, prev_close), 2),
        sma20=_round(sma20),
        sma50=_round(sma50),
        sma200=_round(sma200),
        ema21=_round(ema21),
        rsi14=_round(rsi14),
        macd=_round(macd_line[-1]),
        macd_signal=_round(macd_signal[-1]),
        macd_hist=_round(macd_hist[-1]),
        macd_hist_prev=_round(macd_hist[-2] if len(macd_hist) > 1 else None),
        atr14=_round(atr14),
        atr_pct=round(_pct_change(last_close, last_close - (atr14 or 0)) if atr14 else 0.0, 2),
        high10=round(high10, 2),
        low10=round(low10, 2),
        high20=round(high20, 2),
        low20=round(low20, 2),
        high52=round(high52, 2),
        low52=round(low52, 2),
        dist_high20_pct=round(_pct_change(last_close, high20), 2),
        dist_low20_pct=round(_pct_change(last_close, low20), 2),
        avg_vol20=float(int(avg_vol20)),
        vol_ratio=round(vol_ratio, 2),
        trend=trend,
        signals=signals,
    )


def _round(value: Number) -> Number:
    return None if value is None else round(float(value), 4)


def series_for_chart(bars: Sequence[Bar], last_n: int = 90) -> dict[str, Any]:
    """Дані для SVG-графіка: свічки + ковзні середні."""
    trimmed = list(bars)[-last_n:]
    closes = [b.close for b in bars]
    return {
        "bars": trimmed,
        "sma20": sma_series(closes, 20)[-len(trimmed):],
        "sma50": sma_series(closes, 50)[-len(trimmed):],
    }
