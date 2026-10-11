"""Завантаження конфігурації: config.yaml + .env, з перекриттям через змінні середовища."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


DEFAULTS: dict[str, Any] = {
    "engine": {
        "mode": "auto",
        "llm_provider": "openai",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "max_debate_rounds": 1,
        "max_risk_rounds": 1,
        "analysts": ["market", "news", "fundamentals"],
        "output_language": "Ukrainian",
        "temperature": 0.2,
    },
    "watchlist": ["NVDA", "AAPL", "MSFT", "SPY"],
    "data": {
        "sources": ["yahoo", "twelvedata", "alphavantage"],
        "cache_ttl_min": 240,
        "lookback_days": 420,
        "offline_fallback": "synthetic",
    },
    "llm": {
        "provider": "none",
        "model": None,
        "backend_url": None,
        "temperature": 0.3,
        "max_tokens": 400,
        "timeout_sec": 60,
    },
    "proposal": {
        "risk_per_trade_pct": 0.5,
        "min_rr": 2.0,
        "atr_stop_mult": 1.5,
        "entry_pullback_atr": 0.5,
        "max_pullback_atr": 3.0,
        "max_atr_pct": 6.0,
        "max_stop_pct": 8.0,
        "rsi_buy_range": [42, 72],
        "max_gap_pct": 1.0,
        "premarket_check": True,
        "max_gap_down_pct": 2.0,
    },
    "portfolio": {"currency": "USD", "cash": 10000.0, "prev_close_equity": 10000.0, "positions": []},
    "risk": {
        "max_position_pct": 2.0,
        "max_positions": 8,
        "daily_loss_limit_pct": 3.0,
        "min_confidence": 0.55,
        "allow_short": False,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 20.0,
        "cooldown_days": 3,
        "max_orders_per_day": 3,
    },
    "schedule": {
        "enabled": True,
        # anchor: pre_open (за годину до відкриття), after_close (після закриття), both
        "anchor": "pre_open",
        "market_timezone": "America/New_York",
        "pre_open_cron": "30 8 * * 1-5",     # 8:30 ET = рівно 60 хв до відкриття, цілий рік
        "after_close_cron": "15 23 * * 1-5",  # 16:15 ET = 23:15 за Києвом
        "after_close_timezone": "Europe/Kyiv",
        "display_timezone": "Europe/Kyiv",   # як показувати час у дашборді (сервер може бути в UTC)
        "settle_before_run": True,
        "notify_summary": True,
    },
    "notify": {"telegram": True, "only_actionable": True, "notify_on_error": True},
}


@dataclass
class Settings:
    config: dict[str, Any]
    base_dir: Path
    data_dir: Path
    db_path: Path
    reports_dir: Path
    outbox_path: Path
    cache_dir: Path
    env: dict[str, str] = field(default_factory=dict)

    # ── зручні аксесори ──
    @property
    def engine(self) -> dict[str, Any]:
        return self.config["engine"]

    @property
    def data_cfg(self) -> dict[str, Any]:
        return self.config["data"]

    @property
    def llm_cfg(self) -> dict[str, Any]:
        return self.config["llm"]

    @property
    def proposal_cfg(self) -> dict[str, Any]:
        return self.config["proposal"]

    @property
    def risk(self) -> dict[str, Any]:
        return self.config["risk"]

    def env_or(self, name: str, default: str = "") -> str:
        """Значення з .env/середовища (єдине місце, щоб не смикати os.environ по коду)."""
        import os as _os

        return _os.getenv(name, default)

    @property
    def schedule(self) -> dict[str, Any]:
        return self.config["schedule"]

    @property
    def depth(self) -> dict[str, Any]:
        """Налаштування глибини; ключі з config.yaml перекривають типові."""
        merged = {
            "enabled": True,
            "fundamentals": True,
            "news": True,
            "backtest": True,
            "max_hold_days": 10,
            "calendar_days": 4,
            "min_signals": 8,
            "min_expectancy_r": 0.0,
            "event_block_days": 3,
            "max_per_sector": 1,
            "rs_window": 20,
            "news_count": 5,
            "digest_llm": True,
            "macro": {
                "vix_block": 30,
                "vix_caution": 22,
                "risk_off_risk_factor": 0.5,
                "breadth_min_pct": 40,
            },
        }
        found = self.config.get("depth") or {}
        for key, value in found.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    @property
    def notify_cfg(self) -> dict[str, Any]:
        return self.config["notify"]

    @property
    def watchlist(self) -> list[str]:
        return [t.strip().upper() for t in self.config.get("watchlist", []) if t and t.strip()]

    def portfolio(self) -> dict[str, Any]:
        """data/book.json має пріоритет над config.yaml — так зручніше вести реальний портфель."""
        book_file = self.data_dir / "book.json"
        if book_file.exists():
            import json

            try:
                data = json.loads(book_file.read_text(encoding="utf-8"))
                return {**self.config.get("portfolio", {}), **data}
            except (json.JSONDecodeError, OSError):
                pass
        return dict(self.config.get("portfolio", {}))

    def schedule_jobs(self) -> list[dict[str, str]]:
        """Активні вікна запуску: [(назва, cron, таймзона, опис)]."""
        cfg = self.schedule
        if not cfg.get("enabled", True):
            return []
        anchor = str(cfg.get("anchor", "pre_open")).lower()
        jobs: list[dict[str, str]] = []
        if anchor in ("pre_open", "both"):
            jobs.append(
                {
                    "id": "pre_open",
                    "cron": str(cfg.get("pre_open_cron", "30 8 * * 1-5")),
                    "timezone": str(cfg.get("market_timezone", "America/New_York")),
                    "title": "За годину до відкриття US-ринку",
                }
            )
        if anchor in ("after_close", "both"):
            jobs.append(
                {
                    "id": "after_close",
                    "cron": str(cfg.get("after_close_cron", "15 23 * * 1-5")),
                    "timezone": str(cfg.get("after_close_timezone", "Europe/Kyiv")),
                    "title": "Після закриття (план на наступну сесію)",
                }
            )
        return jobs

    def schedule_summary(self) -> str:
        jobs = self.schedule_jobs()
        if not jobs:
            return "розклад вимкнено (schedule.enabled: false)"
        parts = [f"{job['title']}: cron «{job['cron']}» ({job['timezone']})" for job in jobs]
        return " · ".join(parts)

    def llm_key_present(self) -> bool:
        """Чи є ключ для обраного провайдера (щоб вирішити auto → real/mock)."""
        provider = str(self.engine.get("llm_provider", "")).lower()
        mapping = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "xai": "XAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "groq": "GROQ_API_KEY",
            "moonshot": "MOONSHOT_API_KEY",
            "nvidia": "NVIDIA_API_KEY",
        }
        if provider in ("ollama", "openai_compatible"):
            return True  # локальні сервери працюють без ключа
        key = mapping.get(provider)
        return bool(key and os.getenv(key))


def load_settings(config_path: str | Path | None = None) -> Settings:
    load_dotenv(BASE_DIR / ".env", override=False)

    path = Path(config_path) if config_path else BASE_DIR / "config.yaml"
    file_cfg: dict[str, Any] = {}
    if path.exists():
        file_cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    config = _deep_merge(DEFAULTS, file_cfg)

    # перекриття з середовища
    legacy_cron = (file_cfg.get("schedule") or {}).get("cron")
    if legacy_cron and not (file_cfg.get("schedule") or {}).get("after_close_cron"):
        # старий формат конфіга: лишаємо обидва вікна, щоб нічого не загубилось
        config["schedule"]["after_close_cron"] = legacy_cron
        if not (file_cfg.get("schedule") or {}).get("anchor"):
            config["schedule"]["anchor"] = "both"

    if os.getenv("ASSISTANT_ENGINE_MODE"):
        config["engine"]["mode"] = os.environ["ASSISTANT_ENGINE_MODE"]
    if os.getenv("TRADINGAGENTS_LLM_PROVIDER"):
        config["engine"]["llm_provider"] = os.environ["TRADINGAGENTS_LLM_PROVIDER"]
    if os.getenv("ASSISTANT_NO_SCHEDULE") == "1":
        config["schedule"]["enabled"] = False
    if os.getenv("ASSISTANT_LLM_PROVIDER"):
        config["llm"]["provider"] = os.environ["ASSISTANT_LLM_PROVIDER"]
    if os.getenv("ASSISTANT_LLM_MODEL"):
        config["llm"]["model"] = os.environ["ASSISTANT_LLM_MODEL"]

    data_dir = Path(os.getenv("ASSISTANT_DATA_DIR", BASE_DIR / "data"))
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = data_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        config=config,
        base_dir=BASE_DIR,
        data_dir=data_dir,
        db_path=data_dir / "assistant.db",
        reports_dir=data_dir / "reports",
        outbox_path=data_dir / "outbox.log",
        cache_dir=cache_dir,
    )
