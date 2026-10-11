"""Заглушка TradingAgentsGraph: повертає фіксоване рішення у форматі, який
normalize_decision() уміє читати, і пише файл звіту."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path


class TradingAgentsGraph:
    def __init__(self, selected_analysts=None, debug=False, config=None, **kwargs):
        self.selected_analysts = selected_analysts or ["market", "news", "fundamentals"]
        self.debug = debug
        self.config = config or {}
        self.results_dir = Path(self.config.get("results_dir", "results"))

    def propagate(self, ticker: str, trade_date: str, portfolio=None, **kwargs):
        seed = int(hashlib.sha256(f"stub|{ticker}|{trade_date}".encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        decision = {
            "rating": rng.choice(["Buy", "Hold", "Sell"]),
            "confidence": round(rng.uniform(0.5, 0.85), 2),
            "reference_price": round(rng.uniform(80, 300), 2),
            "rationale": f"[DEV STUB] Аналіз {ticker} на {trade_date}. Справжній рушій не встановлено.",
        }
        state = {
            "company_of_interest": ticker,
            "trade_date": trade_date,
            "final_trade_decision": decision,
            "portfolio": portfolio,
        }
        return state, decision

    def save_reports(self, state, ticker: str) -> Path:
        folder = self.results_dir / ticker
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "complete_report.md"
        path.write_text(
            f"# {ticker} (DEV STUB)\n\n{state.get('final_trade_decision')}\n", encoding="utf-8"
        )
        return path

    def settle_all_pending(self):
        return []
