"""Адаптер до TradingAgents.

Два рушії з однаковим інтерфейсом:
  • TradingAgentsEngine — реальний аналіз через TradingAgentsGraph
  • MockEngine          — детермінована імітація, щоб перевірити всю обв'язку
                          без ключів, без витрат на токени і без мережі
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Any

from .models import (
    RATING_TO_ACTION,
    RATING_TO_CONFIDENCE,
    Action,
    Signal,
)


@dataclass
class EngineResult:
    signal: Signal
    report_markdown: str = ""
    report_html: str | None = None


class EngineError(RuntimeError):
    pass


# ─── нормалізація виходу агента ───────────────────────────────────────────────

def _action_from_text(text: str) -> Action | None:
    low = text.strip().lower()
    for rating, action in RATING_TO_ACTION.items():
        if rating in low:
            return action
    for action in ("buy", "sell", "hold"):
        if action in low:
            return action.upper()  # type: ignore[return-value]
    return None


def normalize_decision(raw: Any, ticker: str, analysis_date: str) -> Signal:
    """Приводить будь-який вихід агента (dict / str / pydantic) до Signal."""
    action: Action | None = None
    rating: str | None = None
    confidence: float | None = None
    rationale = ""
    price: float | None = None

    if isinstance(raw, dict):
        rating = str(raw.get("rating") or raw.get("action") or raw.get("decision") or "").strip() or None
        for key in ("confidence", "conviction", "score"):
            if raw.get(key) is not None:
                try:
                    value = float(raw[key])
                    confidence = value / 100 if value > 1 else value
                    break
                except (TypeError, ValueError):
                    pass
        rationale = str(
            raw.get("rationale") or raw.get("reasoning") or raw.get("thesis") or raw.get("summary") or ""
        )
        for key in ("reference_price", "price", "last_price", "close"):
            if raw.get(key) is not None:
                try:
                    price = float(raw[key])
                    break
                except (TypeError, ValueError):
                    pass
    else:
        rating = str(raw).strip() or None
        rationale = str(raw)

    if rating:
        if rating.lower() in RATING_TO_ACTION:
            action = RATING_TO_ACTION[rating.lower()]
        else:
            action = _action_from_text(rating)

    if action is None:
        raise EngineError(f"не вдалося визначити дію з виходу агента: {raw!r}")

    if confidence is None:
        confidence = RATING_TO_CONFIDENCE.get((rating or "").lower(), 0.5)

    return Signal(
        ticker=ticker.upper(),
        analysis_date=analysis_date,
        action=action,
        rating=rating,
        confidence=max(0.0, min(1.0, confidence)),
        rationale=rationale[:4000],
        reference_price=price,
        raw=raw if isinstance(raw, (dict, str)) else str(raw),
    )


def _find_latest_report(root: Path, ticker: str) -> Path | None:
    if not root.exists():
        return None
    files = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".md", ".html") and ticker.upper() in str(p).upper()
    ]
    if not files:
        files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".html")]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


# ─── реальний рушій ───────────────────────────────────────────────────────────

class TradingAgentsEngine:
    name = "tradingagents"

    def __init__(self, engine_cfg: dict[str, Any], data_dir: Path):
        try:
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.graph.trading_graph import TradingAgentsGraph
        except ImportError as exc:  # pragma: no cover
            raise EngineError(
                "TradingAgents не встановлений. Виконайте:\n"
                "  git clone https://github.com/TauricResearch/TradingAgents.git\n"
                "  cd TradingAgents && pip install ."
            ) from exc

        conf = DEFAULT_CONFIG.copy()
        conf["llm_provider"] = engine_cfg.get("llm_provider", "openai")
        conf["quick_think_llm"] = engine_cfg.get("quick_think_llm")
        conf["deep_think_llm"] = engine_cfg.get("deep_think_llm")
        conf["max_debate_rounds"] = int(engine_cfg.get("max_debate_rounds", 1))
        if engine_cfg.get("max_risk_rounds") is not None:
            conf["max_risk_rounds"] = int(engine_cfg["max_risk_rounds"])
        if engine_cfg.get("output_language"):
            conf["output_language"] = engine_cfg["output_language"]
        if engine_cfg.get("temperature") is not None:
            conf["temperature"] = float(engine_cfg["temperature"])
        conf["results_dir"] = str(data_dir / "ta_results")
        self.conf = conf
        self.results_dir = Path(conf["results_dir"])
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.analysts = list(engine_cfg.get("analysts") or ["market", "news", "fundamentals"])

        self.ta = TradingAgentsGraph(
            selected_analysts=self.analysts,
            debug=False,
            config=conf,
        )

    def analyse(self, ticker: str, analysis_date: str, portfolio: dict[str, Any] | None = None) -> EngineResult:
        from tradingagents.portfolio import PortfolioContext

        kwargs: dict[str, Any] = {}
        if portfolio:
            kwargs["portfolio"] = PortfolioContext.model_validate(portfolio)

        state, decision = self.ta.propagate(ticker.upper(), analysis_date, **kwargs)
        signal = normalize_decision(decision, ticker, analysis_date)

        markdown = ""
        html: str | None = None
        try:
            self.ta.save_reports(state, ticker.upper())
        except Exception:  # звітність не має валити аналіз
            pass
        latest = _find_latest_report(self.results_dir, ticker)
        if latest:
            if latest.suffix.lower() == ".html":
                html = latest.read_text(encoding="utf-8", errors="replace")
                sibling = latest.with_suffix(".md")
                markdown = sibling.read_text(encoding="utf-8", errors="replace") if sibling.exists() else ""
            else:
                markdown = latest.read_text(encoding="utf-8", errors="replace")

        if not markdown:
            markdown = _fallback_markdown(signal, engine=self.name)
        return EngineResult(signal=signal, report_markdown=markdown, report_html=html)

    def settle_all_pending(self) -> list[Any]:
        """Закрити старі рішення по реалізованій alpha (якщо API доступне)."""
        for method in ("settle_all_pending", "settle_pending"):
            fn = getattr(self.ta, method, None)
            if callable(fn):
                try:
                    return list(fn() or [])
                except Exception:
                    return []
        return []


# ─── mock-рушій ───────────────────────────────────────────────────────────────

_BASE_PRICES = {
    "NVDA": 182.4, "AAPL": 231.8, "MSFT": 512.6, "SPY": 640.2, "QQQ": 578.9,
    "TSLA": 331.5, "AMZN": 224.7, "GOOGL": 208.3, "META": 742.1, "AMD": 168.2,
}


class MockEngine:
    """Детермінований псевдо-агент: той самий тікер+дата → той самий результат.

    Потрібен, щоб перевірити весь конвеєр (журнал, ризик-гейт, сповіщення,
    дашборд) без ключів і без витрат. Не використовуйте для реальних рішень.
    """

    name = "mock"

    def __init__(self, engine_cfg: dict[str, Any], data_dir: Path):
        self.cfg = engine_cfg
        self.data_dir = data_dir

    def analyse(self, ticker: str, analysis_date: str, portfolio: dict[str, Any] | None = None) -> EngineResult:
        ticker = ticker.upper()
        seed = int(hashlib.sha256(f"{ticker}|{analysis_date}".encode()).hexdigest()[:12], 16)
        rng = random.Random(seed)

        roll = rng.random()
        if roll < 0.34:
            action, rating = "BUY", rng.choice(["Buy", "Overweight"])
        elif roll < 0.58:
            action, rating = "HOLD", "Hold"
        else:
            action, rating = "SELL", rng.choice(["Sell", "Underweight"])

        base = _BASE_PRICES.get(ticker, 100.0)
        price = round(base * rng.uniform(0.94, 1.06), 2)
        confidence = round(rng.uniform(0.52, 0.88), 2)

        rationale = (
            f"[MOCK] {ticker} на {analysis_date}: {action}. "
            "Технічна картина: ковзні середні без чіткого тренду, RSI у нейтральній зоні. "
            "Фундаментал: оцінка в межах історичного діапазону, маржа стабільна. "
            "Новини: фон нейтрально-позитивний, без подій, що ламають сценарій. "
            "Ризик: висока кореляція з ширшим ринком. "
            "Це імітація для перевірки конвеєра — не аналітичний висновок."
        )

        signal = Signal(
            ticker=ticker,
            analysis_date=analysis_date,
            action=action,
            rating=rating,
            confidence=confidence,
            rationale=rationale,
            reference_price=price,
            raw={"engine": "mock", "seed": seed},
        )
        markdown = _fallback_markdown(signal, engine="mock", seed=seed)
        return EngineResult(signal=signal, report_markdown=markdown, report_html=None)

    def settle_all_pending(self) -> list[Any]:
        return []


def _fallback_markdown(signal: Signal, engine: str, seed: int | None = None) -> str:
    lines = [
        f"# {signal.ticker} — {signal.analysis_date}",
        "",
        f"- **Рушій:** {engine}" + (f" (seed {seed})" if seed is not None else ""),
        f"- **Рішення:** {signal.action}" + (f" ({signal.rating})" if signal.rating else ""),
        f"- **Впевненість:** {signal.confidence:.2f}",
        f"- **Ціна в аналізі:** {signal.reference_price if signal.reference_price else '—'}",
        "",
        "## Обґрунтування",
        "",
        signal.rationale or "—",
    ]
    return "\n".join(lines)


# ─── фабрика ──────────────────────────────────────────────────────────────────

def build_engine(settings) -> tuple[Any, str | None]:
    """Повертає (рушій, попередження). auto → real, якщо є пакет і ключ."""
    mode = str(settings.engine.get("mode", "auto")).lower()

    if mode == "mock":
        return MockEngine(settings.engine, settings.data_dir), None

    try:
        engine = TradingAgentsEngine(settings.engine, settings.data_dir)
    except EngineError as exc:
        if mode == "real":
            raise
        return MockEngine(settings.engine, settings.data_dir), (
            f"Увімкнено mock-режим: {exc}"
        )

    if mode == "auto" and not settings.llm_key_present():
        warning = (
            f"Немає API-ключа для провайдера «{settings.engine.get('llm_provider')}» — "
            "увімкнено mock-режим. Додайте ключ у .env і перезапустіть."
        )
        return MockEngine(settings.engine, settings.data_dir), warning

    return engine, None
