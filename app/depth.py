"""Глибина аналізу: фундаментальні дані, події, режим ринку, сектори, історія правила.

Це шар, який перетворює «план по свічках» на рішення з контекстом:
  • фундаментальні показники (SEC) — чи зростає бізнес;
  • календар звітностей — чи не входимо ми прямо перед звітом;
  • режим ринку (VIX, індекс, ширина) — чи варто взагалі ризикувати сьогодні;
  • сектор і диверсифікація — щоб не набрати три напівпровідники в один день;
  • сила відносно SPY — чи акція справді сильніша за ринок;
  • історична якість правила на цьому тікері (модуль backtest).

Головний принцип збережено: усі числа й рішення рахує код за таблицею порогів
із `config.yaml` (блок `depth`), LLM до них не допущений.
"""

from __future__ import annotations

import logging
import time as time_module
from typing import Any, Sequence

from concurrent.futures import ThreadPoolExecutor

from . import backtest as backtest_module
from . import fundamentals as fundamentals_module
from . import macro as macro_module
from . import news as news_module

log = logging.getLogger("assistant.depth")

# Сектори списку (для диверсифікації). Решта тікерів — «інше».
SECTOR_BY_TICKER = {
    "AAPL": "tech", "MSFT": "tech", "NVDA": "semis", "AMD": "semis", "AVGO": "semis",
    "ORCL": "software", "CRM": "software", "GOOGL": "internet", "META": "internet",
    "AMZN": "consumer", "COST": "consumer", "NFLX": "media", "TSLA": "auto",
    "JPM": "finance", "V": "finance", "UNH": "health", "LLY": "health", "XOM": "energy",
    "SPY": "index", "QQQ": "index",
}
SECTOR_LABELS = {
    "tech": "технології", "semis": "напівпровідники", "software": "ПЗ", "internet": "інтернет",
    "consumer": "споживчий сектор", "media": "медіа", "auto": "автовиробники",
    "finance": "фінанси", "health": "охорона здоров'я", "energy": "енергетика",
    "index": "індексний ETF", "other": "інше",
}


def sector_of(ticker: str) -> str:
    return SECTOR_BY_TICKER.get(ticker.upper(), "other")


def sector_label(ticker: str) -> str:
    return SECTOR_LABELS.get(sector_of(ticker), SECTOR_LABELS["other"])


def relative_strength(bars: Sequence[Any], spy_bars: Sequence[Any] | None, window: int = 20) -> float | None:
    """Наскільки тікер сильніший за SPY за останні `window` сесій, у відсоткових пунктах."""
    if not spy_bars or len(bars) <= window or len(spy_bars) <= window:
        return None
    try:
        own = (bars[-1].close - bars[-1 - window].close) / bars[-1 - window].close * 100
        index = (spy_bars[-1].close - spy_bars[-1 - window].close) / spy_bars[-1 - window].close * 100
    except (AttributeError, IndexError, ZeroDivisionError):
        return None
    return round(own - index, 2)


def correlate(bars_a: Sequence[Any], bars_b: Sequence[Any], window: int = 60) -> float | None:
    """Кореляція денних змін (для попередження про дублювання ризику)."""
    if len(bars_a) < window + 1 or len(bars_b) < window + 1:
        return None
    a = [bars_a[-i].close / bars_a[-i - 1].close - 1 for i in range(window, 0, -1)]
    b = [bars_b[-i].close / bars_b[-i - 1].close - 1 for i in range(window, 0, -1)]
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= 0 or var_b <= 0:
        return None
    return round(cov / (var_a ** 0.5 * var_b ** 0.5), 2)


def warm(settings, tickers: Sequence[str], calendar: dict[str, list[dict[str, Any]]] | None = None,
         *, workers: int = 6) -> None:
    """Прогріває кеш фундаменталу й новин по всьому списку ПАРАЛЕЛЬНО.

    Це найдовша частина глибини (SEC + RSS), тому тягнемо її один раз і паралельно,
    а далі кожен тікер читає вже готовий кеш. Кеш живе добу — наступні прогони
    того ж дня не ходять у мережу взагалі.
    """
    cfg = settings.depth
    jobs = []
    if cfg.get("fundamentals", True):
        jobs += [("fund", ticker) for ticker in tickers]
    if cfg.get("news", True):
        jobs += [("news", ticker) for ticker in tickers]
    if not jobs:
        return
    started = time_module.perf_counter()

    def run(job: tuple[str, str]) -> None:
        kind, ticker = job
        try:
            if kind == "fund":
                fundamentals_module.fetch_fundamentals(settings, ticker, calendar=calendar)
            else:
                news_module.headlines(ticker, settings, limit=int(cfg.get("news_count", 5)))
        except Exception as exc:  # noqa: BLE001 — окремий тікер не має ламати прогін
            log.debug("прогрів %s/%s: %s", kind, ticker, exc)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(run, jobs))
    log.info("глибина: кеш прогріто для %d тікерів за %.1f с", len(tickers), time_module.perf_counter() - started)


def build_context(settings, ticker_bars: dict[str, Any] | None = None) -> dict[str, Any]:
    """Спільний контекст прогону: режим ринку, ширина, календар звітів, свічки SPY.

    Обчислюється ОДИН раз на весь список (у pipeline.build_proposals), тому режим
    ринку й календар не смикають мережу 20 разів.
    """
    cfg = settings.depth
    bars_map = dict(ticker_bars or {})
    spy_bars = bars_map.get("SPY")
    if spy_bars is None:
        try:
            from . import market as market_module

            fetched = market_module.fetch_daily("SPY", settings)
            spy_bars = fetched.bars if fetched else None
            if spy_bars:
                bars_map.setdefault("SPY", spy_bars)
        except Exception as exc:  # noqa: BLE001
            log.debug("немає свічок SPY для макро-контексту: %s", exc)
            spy_bars = None

    breadth_pct = macro_module.breadth(bars_map) if bars_map else None
    macro = macro_module.collect(settings, spy_bars, breadth_pct)
    calendar: dict[str, list[dict[str, Any]]] = {}
    if cfg.get("fundamentals", True):
        try:
            calendar = fundamentals_module.earnings_calendar(
                settings, days_ahead=int(cfg.get("calendar_days", 10))
            )
        except Exception as exc:  # noqa: BLE001 — календар не критичний
            log.debug("календар звітностей недоступний: %s", exc)
    return {
        "spy_bars": spy_bars,
        "macro": macro,
        "breadth_pct": breadth_pct,
        "calendar": calendar,
        "_bars": bars_map,
    }


def collect(
    ticker: str,
    settings,
    *,
    bars: Sequence[Any],
    spy_bars: Sequence[Any] | None,
    macro: dict[str, Any] | None,
    calendar: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Збирає все, що потрібно для глибокого рішення по тікеру (мережа + кеш)."""
    cfg = settings.depth
    packet: dict[str, Any] = {
        "sector": sector_of(ticker),
        "sector_label": sector_label(ticker),
        "rs_20_pct": relative_strength(bars, spy_bars, window=int(cfg.get("rs_window", 20))),
        "macro": macro or {},
        "notes": [],
    }

    packet["fundamentals"] = (
        fundamentals_module.fetch_fundamentals(settings, ticker, calendar=calendar)
        if cfg.get("fundamentals", True) else None
    )
    packet["news"] = (
        news_module.headlines(ticker, settings, limit=int(cfg.get("news_count", 5)))
        if cfg.get("news", True) else []
    )
    packet["backtest"] = (
        backtest_module.evaluate(ticker, bars, settings, max_hold_days=int(cfg.get("max_hold_days", 10)))
        if cfg.get("backtest", True) else {"signals": 0}
    )
    return packet


def deep_verdict(
    *,
    bias: str,
    packet: dict[str, Any],
    settings,
    approved_sectors: dict[str, int] | None = None,
    held_tickers: set[str] | None = None,
) -> dict[str, Any]:
    """Детерміноване рішення шару глибини: {allowed, blocks, notes, risk_factor}.

    Повертає причини блокування у тих самих словах, що й ризик-гейт, тому вони
    зливаються в один список у пропозиції.
    """
    cfg = settings.depth
    macro_cfg = cfg.get("macro", {}) or {}
    blocks: list[str] = []
    notes: list[str] = []
    risk_factor = 1.0

    if bias != "BUY":
        return {"allowed": True, "blocks": [], "notes": [], "risk_factor": 1.0}

    # ── 1. Подія: звітність поруч ──
    fundamentals = packet.get("fundamentals") or {}
    earnings = (fundamentals.get("earnings") or {}) if fundamentals else {}
    days_to = earnings.get("days_to")
    event_days = int(cfg.get("event_block_days", 3))
    if days_to is not None and 0 <= days_to <= event_days:
        approx = " — оцінка за каденцією SEC, не підтверджена біржа" if earnings.get("source") == "sec_estimate" else ""
        blocks.append(
            f"звітність за {days_to} дн. ({earnings.get('date')}){approx}: нові входи перед звітом "
            f"заборонено (ліміт {event_days} дн.)"
        )
    elif days_to is not None and 0 <= days_to <= event_days + 4:
        notes.append(f"звітність через {days_to} дн. — закладайте ризик новин")

    # ── 2. Історична якість правила ──
    stats = packet.get("backtest") or {}
    quality = backtest_module.verdict(
        stats,
        min_signals=int(cfg.get("min_signals", 8)),
        min_expectancy_r=float(cfg.get("min_expectancy_r", 0.0)),
    )
    if quality["state"] == "poor":
        blocks.append(quality["note"])
    else:
        notes.append(quality["note"])

    # ── 3. Режим ринку ──
    state = (packet.get("macro") or {}).get("state", "neutral")
    vix = ((packet.get("macro") or {}).get("inputs") or {}).get("vix")
    vix_block = macro_cfg.get("vix_block")
    if vix is not None and vix_block and vix >= float(vix_block):
        blocks.append(f"VIX {vix:.1f} ≥ {float(vix_block):.0f} — панічний режим, нові входи заборонені")
    elif state == "risk_off":
        risk_factor = float(macro_cfg.get("risk_off_risk_factor", 0.5))
        notes.append(f"режим ринку risk-off: ризик на угоду ×{risk_factor}")

    # ── 4. Диверсифікація за секторами ──
    max_per_sector = int(cfg.get("max_per_sector", 1))
    sector = packet.get("sector", "other")
    used = dict(approved_sectors or {})
    if max_per_sector and used.get(sector, 0) >= max_per_sector and sector not in ("index",):
        blocks.append(
            f"у секторі «{packet.get('sector_label')}» вже схвалено {used[sector]} "
            f"(ліміт {max_per_sector}) — диверсифікація"
        )
    elif used.get(sector):
        notes.append(f"у секторі «{packet.get('sector_label')}» вже є схвалена позиція")

    # ── 5. Довідкові спостереження ──
    if (packet.get("levels") or {}).get("divergence") == "bearish":
        risk_factor = round(risk_factor * 0.75, 3)
        notes.append("ведмежа дивергенція RSI — тиск покупців слабшає, ризик ×0.75")

    rs = packet.get("rs_20_pct")
    if rs is not None:
        if rs > 2:
            notes.append(f"сильніша за SPY на {rs:+.1f} п.п. за 20 сесій")
        elif rs < -2:
            notes.append(f"слабша за SPY на {rs:+.1f} п.п. за 20 сесій")
    fundamentals_score = (fundamentals or {}).get("score")
    if isinstance(fundamentals_score, int) and fundamentals_score <= -2:
        notes.append("фундаментальний фон слабкий (виручка/прибуток/маржа)")

    packet["quality"] = quality
    return {"allowed": not blocks, "blocks": blocks, "notes": notes, "risk_factor": risk_factor}
