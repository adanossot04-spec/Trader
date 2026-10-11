"""Наскрізний тест конвеєра в mock-режимі: аналіз → гейт → журнал → звіт."""

from __future__ import annotations

from pathlib import Path

from app import pipeline
from app.db import Store
from app.engine import MockEngine, normalize_decision


def test_normalize_decision_from_dict():
    signal = normalize_decision(
        {"rating": "Buy", "confidence": 72, "rationale": "чомусь", "price": 182.4},
        "nvda",
        "2026-10-10",
    )
    assert signal.action == "BUY"
    assert signal.ticker == "NVDA"
    assert signal.confidence == 0.72          # 72 → 0.72
    assert signal.reference_price == 182.4


def test_normalize_decision_from_text():
    assert normalize_decision("Strong Sell — тиск на маржу", "AAPL", "2026-10-10").action == "SELL"
    assert normalize_decision("Hold", "AAPL", "2026-10-10").action == "HOLD"


def test_mock_engine_is_deterministic(settings):
    engine = MockEngine(settings.engine, settings.data_dir)
    first = engine.analyse("NVDA", "2026-10-10").signal
    second = engine.analyse("NVDA", "2026-10-10").signal
    third = engine.analyse("NVDA", "2026-10-11").signal
    assert first.model_dump() == second.model_dump()
    assert first.model_dump() != third.model_dump()   # інша дата — інший сценарій


def test_run_one_persists_record_and_report(settings):
    store = Store(settings.db_path)
    record = pipeline.run_one(settings, store, "NVDA", "2026-10-10", notify=True)

    assert record.status == "ok"
    assert record.action in ("BUY", "SELL", "HOLD")
    assert record.rationale
    assert record.report_path and Path(record.report_path).exists()
    assert store.stats()["runs_total"] == 1

    # сповіщення вимкнено (telegram=False), але outbox має отримати запис
    assert settings.outbox_path.exists()


def test_run_watchlist_covers_all_tickers(settings):
    store = Store(settings.db_path)
    records = pipeline.run_watchlist(settings, store, ["NVDA", "AAPL"], summary=False)
    assert [r.ticker for r in records] == ["NVDA", "AAPL"]
    assert store.stats()["runs_total"] == 2


def test_failing_engine_is_recorded_not_raised(settings):
    class Broken(MockEngine):
        name = "broken"

        def analyse(self, ticker, analysis_date, portfolio=None):
            raise RuntimeError("біржа недоступна")

    store = Store(settings.db_path)
    record = pipeline.run_one(settings, store, "TSLA", "2026-10-10", notify=True, engine=Broken(settings.engine, settings.data_dir))
    assert record.status == "error"
    assert "біржа недоступна" in (record.error or "")
    assert store.stats()["runs_error"] == 1
