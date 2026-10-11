"""Історична перевірка правила на самому тікері.

Ідея: перш ніж вірити сьогоднішньому сигналу, подивимося, як це саме правило
працювало на цій самій акції останні ~2 роки. Сигнали відтворюються тим самим
кодом, що й у живому плані (`plan_from_technical`), тому це не «схожий» бектест,
а буквально те саме правило.

Чесні спрощення, які варто пам'ятати:
  • заявка виконується тільки якщо наступний день дає ціну (ліміт — дотиком,
    ринок — за ціною відкриття);
  • якщо стоп і ціль трапляються в одному дні — зараховуємо стоп (консервативно);
  • без урахування дивідендів, спредів, комісій і прослизання;
  • якщо угода не закрита за `max_hold_days` — виходимо за ціною закриття.

Результат — не «прогноз дохідності», а оцінка якості правила: чи має воно
на цьому тікері позитивне очікування в одиницях ризику (R).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import technical as tech_module

log = logging.getLogger("assistant.backtest")

WARMUP_BARS = 210          # SMA200 + запас: сигнали рахуємо лише після розігріву


def _plan(tech, settings):
    """Ліниве підключення правил: proposal імпортує depth, тому циклічний імпорт тут не потрібен."""
    from .proposal import plan_from_technical

    return plan_from_technical(tech, settings, has_position=False)


def simulate(
    ticker: str,
    bars: Sequence[Any],
    settings,
    *,
    max_hold_days: int = 10,
) -> dict[str, Any]:
    """Прогін правила по історії. Чиста функція — мережа не потрібна."""
    if len(bars) < WARMUP_BARS + 20:
        return {"signals": 0, "trades": [], "note": "замало історії для перевірки"}

    trades: list[dict[str, Any]] = []
    for index in range(WARMUP_BARS, len(bars) - 1):
        tech = tech_module.analyze(ticker, bars[: index + 1])          # те саме, що в живому плані
        plan = _plan(tech, settings)
        if plan.get("bias") != "BUY":
            continue

        entry = float(plan["entry"])
        stop = float(plan["stop"])
        target = float(plan["target"])
        nxt = bars[index + 1]

        if plan.get("entry_type") == "market":
            fill = float(nxt.open)
        else:
            if float(nxt.low) > entry:
                continue                                              # ліміт не зачепили
            fill = entry
        if fill <= stop:
            continue

        exit_price = None
        outcome = "time"
        days = 0
        last_index = min(index + max_hold_days, len(bars) - 1)
        for j in range(index + 1, last_index + 1):
            bar = bars[j]
            days = j - index
            if float(bar.low) <= stop:                                # стоп перевіряємо першим
                exit_price, outcome = stop, "stop"
                break
            if float(bar.high) >= target:
                exit_price, outcome = target, "target"
                break
        if exit_price is None:
            exit_price = float(bars[last_index].close)

        risk = fill - stop
        r_multiple = (exit_price - fill) / risk if risk > 0 else 0.0
        trades.append({
            "session": str(bars[index].date),
            "entry": round(fill, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "exit": round(exit_price, 2),
            "outcome": outcome,
            "days": days,
            "r": round(r_multiple, 3),
        })

    return {"signals": len(trades), "trades": trades, **_stats(trades)}


def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"win_rate": None, "expectancy_r": None, "avg_r": None, "profit_factor": None,
                "max_dd_r": None, "avg_days": None, "target_rate": None, "stop_rate": None,
                "best_r": None, "worst_r": None}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in rs:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "win_rate": round(100 * len(wins) / len(rs), 1),
        "expectancy_r": round(sum(rs) / len(rs), 3),
        "avg_r": round(sum(rs) / len(rs), 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_dd_r": round(max_dd, 2),
        "avg_days": round(sum(t["days"] for t in trades) / len(trades), 1),
        "target_rate": round(100 * sum(1 for t in trades if t["outcome"] == "target") / len(trades), 1),
        "stop_rate": round(100 * sum(1 for t in trades if t["outcome"] == "stop") / len(trades), 1),
        "best_r": round(max(rs), 2),
        "worst_r": round(min(rs), 2),
    }


def _cache_path(cache_dir: Path, ticker: str) -> Path:
    folder = cache_dir / "backtest"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{ticker.upper()}.json"


def evaluate(ticker: str, bars: Sequence[Any], settings, *, max_hold_days: int = 10) -> dict[str, Any]:
    """Те саме з кешем за останньою сесією (перераховується раз на день)."""
    if not bars:
        return {"signals": 0, "trades": []}
    key = f"{bars[-1].date}|{max_hold_days}|{len(bars)}"
    path = _cache_path(settings.cache_dir, ticker)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("key") == key:
                return payload["data"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    data = simulate(ticker, bars, settings, max_hold_days=max_hold_days)
    try:
        path.write_text(
            json.dumps({"key": key, "fetched_at": datetime.now().isoformat(timespec="seconds"),
                        "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return data


def verdict(stats: dict[str, Any], *, min_signals: int = 8, min_expectancy_r: float = 0.0) -> dict[str, Any]:
    """Детермінований висновок про якість правила на цьому тікері."""
    signals = int(stats.get("signals") or 0)
    expectancy = stats.get("expectancy_r")
    if signals < min_signals:
        return {
            "state": "unknown",
            "allowed": True,
            "note": f"мало історичних сигналів ({signals} < {min_signals}) — оцінка ненадійна",
        }
    if expectancy is None:
        return {"state": "unknown", "allowed": True, "note": "немає оцінки очікування"}
    if expectancy < min_expectancy_r:
        return {
            "state": "poor",
            "allowed": False,
            "note": (f"правило на цьому тікері історично слабке: {signals} сигналів, "
                     f"очікування {expectancy:+.2f}R < {min_expectancy_r:+.2f}R"),
        }
    return {
        "state": "good",
        "allowed": True,
        "note": (f"правило підтверджене історією: {signals} сигналів, "
                 f"{stats.get('win_rate')}% успішних, очікування {expectancy:+.2f}R"),
    }
