"""Смоук-тести FastAPI: саме тут ловиться клас помилок «request.state ≠ app.state»."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient


def make_client(settings, monkeypatch):
    import app.web as web

    monkeypatch.setattr(web, "load_settings", lambda: settings)
    return TestClient(web.app)


def test_endpoints_smoke(settings, monkeypatch):
    with make_client(settings, monkeypatch) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/summary").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/settings").status_code == 200
        assert client.get("/docs").status_code == 200
        health = client.get("/api/health").json()
        assert health["data_sources"] == ["synthetic"] and health["llm"] == "none"


def test_watchlist_and_book_endpoints(settings, monkeypatch):
    with make_client(settings, monkeypatch) as client:
        assert client.post("/api/watchlist", json={"ticker": "tsla"}).json()["ok"]
        assert "TSLA" in client.get("/api/watchlist").json()["tickers"]
        assert client.delete("/api/watchlist/TSLA").json()["ok"]
        assert "TSLA" not in client.get("/api/watchlist").json()["tickers"]

        saved = client.post("/api/book", json={"cash": 1234.0, "positions": []}).json()
        assert saved["ok"] and saved["book"]["cash"] == 1234.0
        assert client.get("/api/book").json()["cash"] == 1234.0


def test_run_and_report_pages(settings, monkeypatch):
    from app import pipeline
    from app.db import Store

    store = Store(settings.db_path)
    record = pipeline.run_one(settings, store, "NVDA", "2026-10-10", notify=True)

    with make_client(settings, monkeypatch) as client:
        runs = client.get("/api/runs").json()["runs"]
        assert runs and runs[0]["ticker"] == "NVDA"

        page = client.get(f"/reports/{record.id}")
        assert page.status_code == 200 and "NVDA" in page.text
        assert client.get(f"/reports/{record.id}/raw").status_code == 200
        assert client.get("/reports/9999").status_code == 404


def wait_idle(client, timeout: float = 10.0) -> None:
    """Фонові задачі йдуть у тредах — чекаємо, доки сервіс звільниться."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not client.get("/api/health").json()["running"]:
            return
        time.sleep(0.1)
    raise AssertionError("фонова задача не завершилася вчасно")


def test_trigger_run_is_async(settings, monkeypatch):
    with make_client(settings, monkeypatch) as client:
        response = client.post("/api/run", json={"tickers": ["AAPL"], "summary": False})
        assert response.status_code == 202 and response.json()["started"] is True
        wait_idle(client)


def test_next_run_text_shows_display_and_market_timezone(settings, monkeypatch):
    import datetime as dt
    from zoneinfo import ZoneInfo

    from app.web import next_run_text

    class FakeJob:
        id = "pre_open"

        class next_run_time:
            # 12.10.2026 12:30 UTC = 08:30 ET = 15:30 Kyiv
            pass

    FakeJob.next_run_time = dt.datetime(2026, 10, 12, 12, 30, tzinfo=dt.timezone.utc)

    class FakeScheduler:
        def get_jobs(self):
            return [FakeJob()]

    text = next_run_text(FakeScheduler(), "Europe/Kyiv")
    assert "12.10 15:30" in text and "Kyiv" in text
    assert "08:30 ET" in text                    # і за Нью-Йорком — щоб не плутатись


def test_propose_endpoint_and_pages(settings, monkeypatch):
    with make_client(settings, monkeypatch) as client:
        wait_idle(client)
        response = client.post("/api/propose", json={"tickers": ["NVDA"], "force_refresh": True})
        assert response.status_code == 202 and response.json()["started"] is True
        wait_idle(client)
        proposals = client.get("/api/proposals").json()["proposals"]
        assert proposals, "пропозиція не з'явилася"
        proposal = proposals[0]
        assert proposal["ticker"] == "NVDA" and proposal["for_session"]

        page = client.get(f"/proposal/{proposal['id']}")
        assert page.status_code == 200 and "NVDA" in page.text and "<svg" in page.text
        assert client.get("/proposal/9999").status_code == 404
        assert client.get(f"/api/proposals/{proposal['id']}").json()["ticker"] == "NVDA"

        summary = client.get("/api/summary").json()
        assert "proposals_total" in summary and summary["next_session"]
