"""Перевірка інтеграційного шляху TradingAgentsEngine через dev-заглушку.

Справжній пакет TradingAgents при цьому не потрібен. Якщо він встановлений,
тест усе одно скористається заглушкою (dev/ додається в sys.path лише тут).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app import pipeline
from app.db import Store
from app.engine import EngineError, TradingAgentsEngine

DEV = Path(__file__).resolve().parent.parent / "dev"


@pytest.fixture
def stub_env(tmp_path):
    sys.path.insert(0, str(DEV))
    for mod in [m for m in sys.modules if m == "tradingagents" or m.startswith("tradingagents.")]:
        del sys.modules[mod]
    importlib.invalidate_caches()
    yield
    for mod in [m for m in sys.modules if m == "tradingagents" or m.startswith("tradingagents.")]:
        del sys.modules[mod]
    sys.path.remove(str(DEV))
    importlib.invalidate_caches()


def test_real_engine_path_with_stub(settings, stub_env):
    settings.config["engine"]["mode"] = "real"
    engine = TradingAgentsEngine(settings.engine, settings.data_dir)
    result = engine.analyse("NVDA", "2026-10-10", portfolio={"cash": 1000.0, "positions": []})

    assert result.signal.action in ("BUY", "SELL", "HOLD")
    assert result.signal.confidence > 0
    assert result.report_markdown                     # звіт знайдено/згенеровано


def test_pipeline_works_on_real_path(settings, stub_env):
    settings.config["engine"]["mode"] = "real"
    settings.config["engine"]["analysts"] = ["market", "news"]
    store = Store(settings.db_path)
    record = pipeline.run_one(settings, store, "AAPL", "2026-10-10", notify=True)
    assert record.status == "ok"
    assert record.engine == "tradingagents"
    assert record.report_path and Path(record.report_path).exists()


def test_real_mode_without_stub_raises(settings):
    """Без заглушки і без встановленого пакета — зрозуміла помилка, а не ImportError."""
    try:
        import tradingagents  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("справжній TradingAgents встановлено — перевірка не потрібна")

    settings.config["engine"]["mode"] = "real"
    with pytest.raises(EngineError) as exc:
        TradingAgentsEngine(settings.engine, settings.data_dir)
    assert "pip install" in str(exc.value) or "не встановлений" in str(exc.value)
