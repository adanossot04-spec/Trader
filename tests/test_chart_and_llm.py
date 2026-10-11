"""SVG-графіки та LLM-коментар (безкоштовні провайдери) — без мережі в тестах."""

from __future__ import annotations

import datetime as dt

import pytest

from app import chart, llm, market
from app.market import Bar
from app.proposal import Proposal


def sample_bars(n: int = 60) -> list[Bar]:
    bars, day, price = [], dt.date(2026, 1, 1), 100.0
    for idx in range(n):
        price += 0.6 if idx % 3 else -0.4
        bars.append(Bar(date=day.isoformat(), open=price - 0.3, high=price + 0.8, low=price - 0.9, close=price, volume=1e6))
        day += dt.timedelta(days=1)
    return bars


# ─── графіки ─────────────────────────────────────────────────────────────────

def test_candles_svg_has_candles_and_levels():
    bars = sample_bars()
    svg = chart.candles_svg(
        bars,
        levels={"entry": 110.0, "stop": 100.0, "target": 125.0},
        sma20=[None] * 10 + [105.0] * 50,
        sma50=[None] * 10 + [102.0] * 50,
        title="TEST · добові свічки",
    )
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<rect") >= len(bars)                       # тіла свічок + обсяги
    assert "Вхід" in svg and "Стоп" in svg and "Ціль" in svg
    assert "SMA20" in svg and "SMA50" in svg
    assert "TEST · добові свічки" in svg


def test_candles_svg_survives_missing_levels_and_empty_data():
    svg = chart.candles_svg(sample_bars(30))
    assert "<svg" in svg and "Вхід" not in svg
    assert chart.candles_svg([]) .startswith("<svg")


def test_candles_svg_has_no_nan():
    svg = chart.candles_svg(sample_bars(), sma20=[None] * 60)
    assert "nan" not in svg.lower()


def test_sparkline():
    line = chart.sparkline_svg([1, 3, 2, 5, 4])
    assert line.startswith("<svg") and "polyline" in line
    assert chart.sparkline_svg([1]) == ""


def test_levels_from_proposal():
    proposal = Proposal(ticker="X", for_session="2026-10-12", last_bar_date="2026-10-09",
                        last_close=100.0, change_pct=0.5, entry_price=98.0, stop_price=95.0, target_price=110.0)
    assert chart.levels_from_proposal(proposal) == {"entry": 98.0, "stop": 95.0, "target": 110.0}


# ─── LLM: вибір провайдера ───────────────────────────────────────────────────

def test_provider_status_none_is_fine(settings):
    status = llm.provider_status(settings)
    assert status["provider"] == "none" and status["key_present"] is True
    assert "0 витрат" in status["hint"]


def test_provider_status_flags_missing_keys(settings, monkeypatch):
    settings.config["llm"]["provider"] = "groq"
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert llm.provider_status(settings)["key_present"] is False

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    status = llm.provider_status(settings)
    assert status["key_present"] is True
    assert status["model"] == "llama-3.3-70b-versatile"          # модель за замовчуванням
    assert status["is_free_tier"] is True


def test_provider_status_google_hint(settings, monkeypatch):
    settings.config["llm"]["provider"] = "google"
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    status = llm.provider_status(settings)
    assert "aistudio" in status["hint"] and status["key_present"] is False


# ─── LLM: виклики ────────────────────────────────────────────────────────────

def test_narrate_disabled_returns_nothing(settings):
    assert llm.narrate({"ticker": "NVDA"}, settings) == (None, None)


def test_narrate_groq_success(settings, monkeypatch):
    settings.config["llm"]["provider"] = "groq"
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Сценарій: тримаємо висхідний тренд."}}]}

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return Response()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    text, error = llm.narrate({"ticker": "NVDA", "rsi14": 55}, settings)
    assert error is None and "Сценарій" in text
    assert captured["url"].startswith("https://api.groq.com/openai/v1/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer test-key"


def test_narrate_google_success(settings, monkeypatch):
    settings.config["llm"]["provider"] = "google"
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "Сценарій від Gemini."}]}}]}

    monkeypatch.setattr(llm.httpx, "post", lambda url, **kwargs: Response())
    text, error = llm.narrate({"ticker": "NVDA"}, settings)
    assert error is None and text == "Сценарій від Gemini."


def test_narrate_returns_error_without_crashing(settings, monkeypatch):
    settings.config["llm"]["provider"] = "groq"

    def boom(*args, **kwargs):
        raise RuntimeError("немає інтернету")

    monkeypatch.setattr(llm.httpx, "post", boom)
    text, error = llm.narrate({"ticker": "NVDA"}, settings)
    assert text is None and "немає інтернету" in error


def test_narrate_unknown_provider(settings):
    settings.config["llm"]["provider"] = "телепатія"
    text, error = llm.narrate({"ticker": "NVDA"}, settings)
    assert text is None and "невідомий провайдер" in error
