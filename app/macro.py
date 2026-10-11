"""Режим ринку без ключів: VIX, 10-річна ставка, долар, ширина ринку.

Навіщо: план на окрему акцію безрозсудний без контексту. У risk-off (VIX високий,
індекс під SMA200, більшість акцій під своїми середніми) навіть гарний сетап
частіше провалюється — тому режим ринку зменшує кількість входів і ризик,
а в risk-off із панікою (VIX вище порогу) нові входи заборонені.

Усе детерміновано: класифікація — це таблиця порогів, а не «думка» моделі.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import httpx

log = logging.getLogger("assistant.macro")

SYMBOLS = {"vix": "^VIX", "ten_year": "^TNX", "dollar": "DX-Y.NYB"}
TTL_MINUTES = 60
_UA = {"User-Agent": "Mozilla/5.0 (compatible; TradingAssistant/0.3)"}


def classify(
    *,
    vix: float | None,
    vix_prev: float | None,
    spy_close: float | None,
    spy_sma50: float | None,
    spy_sma200: float | None,
    ten_year: float | None,
    ten_year_prev: float | None,
    breadth_pct: float | None,
) -> dict[str, Any]:
    """Таблиця порогів → режим ринку. Чиста функція (тестується без мережі)."""
    reasons: list[str] = []
    score = 0

    if vix is not None:
        if vix < 16:
            score += 1
            reasons.append(f"VIX {vix:.1f} — спокійно")
        elif vix <= 25:
            reasons.append(f"VIX {vix:.1f} — звичайна волатильність")
        else:
            score -= 2
            reasons.append(f"VIX {vix:.1f} — страх на ринку")
        if vix_prev:
            change = (vix - vix_prev) / vix_prev * 100
            if change > 15:
                score -= 1
                reasons.append(f"VIX зростає швидко ({change:+.0f}% за сесію)")

    if spy_close and spy_sma200:
        if spy_close > spy_sma200:
            score += 1
            reasons.append("індекс (SPY) над SMA200 — довгостроковий тренд бичачий")
        else:
            score -= 2
            reasons.append("індекс (SPY) під SMA200 — ведмежий тренд")
    if spy_close and spy_sma50:
        if spy_close < spy_sma50:
            score -= 1
            reasons.append("індекс під SMA50 — короткострокова слабкість")

    if breadth_pct is not None:
        if breadth_pct >= 55:
            score += 1
            reasons.append(f"ширина: {breadth_pct:.0f}% тікерів списку над своїми SMA50")
        elif breadth_pct <= 35:
            score -= 1
            reasons.append(f"ширина: лише {breadth_pct:.0f}% тікерів над SMA50 — рух вузький")

    if ten_year is not None and ten_year_prev:
        if ten_year - ten_year_prev > 0.1:
            score -= 1
            reasons.append(f"10-річна ставка росте ({ten_year:.2f}% проти {ten_year_prev:.2f}%)")

    if score >= 2:
        state = "risk_on"
    elif score <= -2:
        state = "risk_off"
    else:
        state = "neutral"

    return {
        "state": state,
        "score": max(-5, min(5, score)),
        "reasons": reasons,
        "inputs": {
            "vix": vix, "vix_prev": vix_prev,
            "spy_close": spy_close, "spy_sma50": spy_sma50, "spy_sma200": spy_sma200,
            "ten_year": ten_year, "ten_year_prev": ten_year_prev,
            "breadth_pct": breadth_pct,
        },
    }


def breadth(bars_by_ticker: dict[str, Sequence[Any]]) -> float | None:
    """Скільки відсотків тікерів списку торгуються над своєю SMA50 (внутрішня ширина)."""
    from .technical import sma_series

    above = total = 0
    for bars in bars_by_ticker.values():
        closes = [bar.close for bar in bars]
        if len(closes) < 55:
            continue
        sma = sma_series(closes, 50)[-1]
        if sma is None:
            continue
        total += 1
        above += 1 if closes[-1] > sma else 0
    if not total:
        return None
    return round(above / total * 100, 1)


def _quote(symbol: str, settings) -> tuple[float | None, float | None]:
    """Останнє й попереднє закриття символу (VIX, ставка, долар) — без ключа."""
    from . import market as market_module

    safe = symbol.replace("^", "_").replace(".", "-").replace("/", "-")
    folder = settings.cache_dir / "macro"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe}.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if datetime.now() - datetime.fromisoformat(payload["fetched_at"]) < timedelta(minutes=TTL_MINUTES):
                return payload.get("last"), payload.get("prev")
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
    try:
        response = httpx.get(url, headers=_UA, timeout=20.0)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        closes = [value for value in result["indicators"]["quote"][0]["close"] if value is not None]
        closes = market_module.completed_sessions_only(
            [market_module.Bar(date=str(i), open=c, high=c, low=c, close=c, volume=0)
             for i, c in enumerate(closes)]
        )
        values = [bar.close for bar in closes]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.debug("макро %s: %s", symbol, exc)
        return None, None

    last = values[-1] if values else None
    prev = values[-2] if len(values) > 1 else None
    try:
        path.write_text(
            json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"),
                        "last": last, "prev": prev}),
            encoding="utf-8",
        )
    except OSError:
        pass
    return last, prev


def collect(settings, spy_bars: Sequence[Any] | None, breadth_pct: float | None) -> dict[str, Any]:
    """Збирає режим ринку (з кешем на годину). Немає мережі — поверне neutral."""
    from .technical import sma_series

    vix, vix_prev = _quote(SYMBOLS["vix"], settings)
    ten_year, ten_year_prev = _quote(SYMBOLS["ten_year"], settings)
    dollar, _ = _quote(SYMBOLS["dollar"], settings)   # для довідки (не впливає на бал)

    spy_close = spy_sma50 = spy_sma200 = None
    if spy_bars:
        closes = [bar.close for bar in spy_bars]
        spy_close = closes[-1]
        if len(closes) >= 50:
            spy_sma50 = sma_series(closes, 50)[-1]
        if len(closes) >= 200:
            spy_sma200 = sma_series(closes, 200)[-1]

    result = classify(
        vix=vix, vix_prev=vix_prev,
        spy_close=spy_close, spy_sma50=spy_sma50, spy_sma200=spy_sma200,
        ten_year=ten_year, ten_year_prev=ten_year_prev,
        breadth_pct=breadth_pct,
    )
    result["inputs"]["dollar"] = dollar          # довідково: у бал режиму не входить
    result["as_of"] = datetime.now().isoformat(timespec="minutes")
    return result
