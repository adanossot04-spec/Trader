"""FastAPI-сервіс: пропозиції на наступну сесію + опційний глибокий аналіз.

Запуск: python run.py   (або uvicorn app.web:app)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import llm as llm_module
from . import market as market_module
from . import notify as notify_module
from . import pipeline, views
from .db import Store
from .scheduler import build_scheduler
from .settings import Settings, load_settings

log = logging.getLogger("assistant.web")

STATE: dict[str, Any] = {
    "running": False,
    "current": None,
    "kind": None,            # proposals | analysis
    "started_at": None,
    "last_error": None,
    "engine": "—",
    "engine_warning": None,
}
_state_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    store = Store(settings.db_path)
    store.seed_watchlist(settings.watchlist)

    app.state.settings = settings
    app.state.store = store

    engine, warning = pipeline.get_engine(settings)
    STATE["engine"] = getattr(engine, "name", "unknown")
    STATE["engine_warning"] = warning
    if warning:
        log.info(warning)

    scheduler = build_scheduler(settings, store)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        # Starlette будує request.state з цього словника — саме тому yield-словник,
        # а не лише app.state.* (request.state ≠ app.state)
        yield {"store": store, "settings": settings}
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Trading Assistant API",
    version="0.2.0",
    description=(
        "Пропозиції на наступну торгову сесію за добовим графіком US-акцій: "
        "безкоштовні джерела даних → детермінований технічний аналіз → ризик-гейт → Telegram/дашборд. "
        "Ордери не виконуються, рішення ухвалює людина."
    ),
    lifespan=lifespan,
)


# ─── фонові задачі ────────────────────────────────────────────────────────────

def next_run_text(scheduler, display_timezone: str = "Europe/Kyiv") -> str | None:
    """Коли спрацює наступний прогін — у зручній таймзоні та за Нью-Йорком."""
    try:
        jobs = [job for job in scheduler.get_jobs() if getattr(job, "next_run_time", None)]
    except Exception:  # noqa: BLE001
        return None
    if not jobs:
        return None
    job = min(jobs, key=lambda item: item.next_run_time)
    moment = job.next_run_time
    try:
        from zoneinfo import ZoneInfo

        local = moment.astimezone(ZoneInfo(display_timezone))
        label = f"{local.strftime('%d.%m %H:%M')} {display_timezone.split('/')[-1]}"
    except Exception:  # noqa: BLE001
        local = moment.astimezone()
        label = f"{local.strftime('%d.%m %H:%M')} {local.strftime('%Z')}"
    et = moment.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")
    return f"{label} (= {et} ET) · {job.id}"


def _kick_off(settings: Settings, store: Store, kind: str, worker) -> bool:
    with _state_lock:
        if STATE["running"]:
            return False
        STATE.update(running=True, kind=kind, current="…", started_at=datetime.now().isoformat(timespec="seconds"))

    def runner() -> None:
        try:
            worker()
        except Exception as exc:  # noqa: BLE001
            log.exception("Фонова задача %s упала", kind)
            STATE["last_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            with _state_lock:
                STATE.update(running=False, current=None, kind=None)

    threading.Thread(target=runner, name=f"assistant-{kind}", daemon=True).start()
    return True


def _propose_in_background(settings: Settings, store: Store, tickers: list[str] | None, force: bool) -> bool:
    def worker() -> None:
        STATE["current"] = ", ".join(tickers) if tickers else "watchlist"
        pipeline.build_proposals(settings, store, tickers, force_refresh=force)

    return _kick_off(settings, store, "proposals", worker)


def _analyse_in_background(settings: Settings, store: Store, tickers: list[str] | None) -> bool:
    def worker() -> None:
        STATE["current"] = ", ".join(tickers) if tickers else "watchlist"
        pipeline.run_watchlist(settings, store, tickers)

    return _kick_off(settings, store, "analysis", worker)


# ─── API ──────────────────────────────────────────────────────────────────────

class ProposeRequest(BaseModel):
    tickers: list[str] | None = None
    all: bool = False
    force_refresh: bool = False


class RunRequest(BaseModel):
    tickers: list[str] | None = None
    all: bool = False
    summary: bool = True


class TickerRequest(BaseModel):
    ticker: str


@app.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return {
        "status": "ok",
        "engine": STATE["engine"],
        "running": STATE["running"],
        "data_sources": settings.data_cfg.get("sources"),
        "llm": settings.llm_cfg.get("provider"),
    }


@app.get("/api/summary")
def summary(request: Request) -> dict[str, Any]:
    store: Store = request.app.state.store
    settings: Settings = request.app.state.settings
    stats = store.stats() | store.proposal_stats()
    session = store.latest_proposal_session()
    stats["next_session"] = session
    stats["watchlist"] = store.watchlist()
    stats["running"] = STATE["running"]
    stats["kind"] = STATE["kind"]
    stats["engine"] = STATE["engine"]
    stats["data_sources"] = settings.data_cfg.get("sources")
    stats["llm_provider"] = settings.llm_cfg.get("provider")
    stats["schedule"] = [job for job in settings.schedule_jobs()]
    stats["next_run"] = next_run_text(
        request.app.state.scheduler, str(settings.schedule.get("display_timezone", "Europe/Kyiv"))
    )
    stats["minutes_to_open"] = market_module.minutes_to_open()
    stats["kill_switch"] = any(
        "KILL-SWITCH" in (p.gate_reasons or [""])[0] for p in (store.proposals_for_session(session) if session else [])
    )
    return stats


@app.get("/api/proposals")
def list_proposals(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    ticker: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    store: Store = request.app.state.store
    if session:
        items = store.proposals_for_session(session)
    else:
        items = store.recent_proposals(limit=limit, ticker=ticker)
    return {"count": len(items), "proposals": [p.to_dict() for p in items]}


@app.get("/api/proposals/{proposal_id}")
def get_proposal(proposal_id: int, request: Request) -> dict[str, Any]:
    proposal = request.app.state.store.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="пропозицію не знайдено")
    return proposal.to_dict()


@app.post("/api/propose")
def trigger_propose(payload: ProposeRequest, request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    tickers = None if payload.all or not payload.tickers else [t.upper() for t in payload.tickers]
    if not _propose_in_background(settings, store, tickers, payload.force_refresh):
        return JSONResponse({"started": False, "detail": "уже виконується інша задача"}, status_code=409)
    return JSONResponse({"started": True, "tickers": tickers or store.watchlist()}, status_code=202)


@app.post("/api/run")
def trigger_run(payload: RunRequest, request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    tickers = None if payload.all or not payload.tickers else [t.upper() for t in payload.tickers]
    if not _analyse_in_background(settings, store, tickers):
        return JSONResponse({"started": False, "detail": "уже виконується інша задача"}, status_code=409)
    return JSONResponse({"started": True, "tickers": tickers or store.watchlist()}, status_code=202)


@app.get("/api/runs")
def list_runs(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    ticker: str | None = None,
) -> dict[str, Any]:
    records = request.app.state.store.recent_runs(limit=limit, ticker=ticker)
    return {"count": len(records), "runs": [r.model_dump() for r in records]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, request: Request) -> dict[str, Any]:
    record = request.app.state.store.get_run(run_id)
    if not record:
        raise HTTPException(status_code=404, detail="прогін не знайдено")
    return record.model_dump()


@app.get("/api/watchlist")
def get_watchlist(request: Request) -> dict[str, Any]:
    return {"tickers": request.app.state.store.watchlist()}


@app.post("/api/watchlist")
def add_watchlist(payload: TickerRequest, request: Request) -> dict[str, Any]:
    ticker = payload.ticker.strip().upper()
    if not ticker or len(ticker) > 12:
        raise HTTPException(status_code=400, detail="некоректний тікер")
    request.app.state.store.add_ticker(ticker)
    return {"ok": True, "tickers": request.app.state.store.watchlist()}


@app.delete("/api/watchlist/{ticker}")
def delete_watchlist(ticker: str, request: Request) -> dict[str, Any]:
    request.app.state.store.remove_ticker(ticker)
    return {"ok": True, "tickers": request.app.state.store.watchlist()}


@app.get("/api/book")
def get_book(request: Request) -> dict[str, Any]:
    return request.app.state.settings.portfolio()


@app.post("/api/book")
async def save_book(payload: dict, request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    path = settings.data_dir / "book.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, TypeError) as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "book": settings.portfolio()}


@app.post("/api/notify/test")
def notify_test(request: Request) -> dict[str, Any]:
    status = notify_module.notify_config_status()
    if not (status["telegram_token"] and status["telegram_chat"]):
        return {"sent": False, "detail": "Telegram не налаштовано: додайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у .env"}
    ok, detail = notify_module.send_telegram(
        "✅ <b>Trading Assistant</b>: тестове повідомлення. Канал працює.",
        os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        os.environ["TELEGRAM_CHAT_ID"].strip(),
    )
    return {"sent": ok, "detail": detail}


# ─── сторінки ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request) -> str:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    session = store.latest_proposal_session()
    proposals = store.proposals_for_session(session) if session else []
    stats = store.stats() | store.proposal_stats()
    stats["watchlist"] = store.watchlist()
    stats["next_session"] = session
    from .risk import Book

    stats["kill_switch"] = Book.from_dict(settings.portfolio()).daily_pnl_pct() <= -abs(
        float(settings.risk.get("daily_loss_limit_pct", 3.0))
    )
    stats["minutes_to_open"] = market_module.minutes_to_open()
    state = dict(STATE)
    state["next_run"] = next_run_text(
        request.app.state.scheduler, str(settings.schedule.get("display_timezone", "Europe/Kyiv"))
    )
    return views.dashboard(
        proposals, store.recent_runs(limit=12), stats, settings, state, STATE.get("engine_warning")
    )


@app.get("/proposal/{proposal_id}", response_class=HTMLResponse)
def proposal_page(proposal_id: int, request: Request) -> str:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    proposal = store.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="пропозицію не знайдено")

    chart_svg = ""
    try:
        market_data = market_module.fetch_daily(proposal.ticker, settings)
        chart_svg = views.chart_for(proposal, market_data.bars)
    except Exception as exc:  # графік не критичний для сторінки
        log.warning("графік для %s не збудовано: %s", proposal.ticker, exc)
        chart_svg = f'<div class="banner">Графік недоступний: {type(exc).__name__}</div>'

    return views.proposal_detail(proposal, chart_svg, settings, STATE.get("engine_warning"))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> str:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    book_json = json.dumps(settings.portfolio(), ensure_ascii=False, indent=2)
    return views.settings_page(
        settings,
        store.stats() | store.proposal_stats(),
        notify_module.notify_config_status(),
        book_json,
        STATE.get("engine_warning"),
        llm_status=llm_module.provider_status(settings),
        data_sources=[str(s) for s in (settings.data_cfg.get("sources") or [])],
    )


@app.get("/reports/{run_id}", response_class=HTMLResponse)
def report_page(run_id: int, request: Request) -> str:
    store: Store = request.app.state.store
    record = store.get_run(run_id)
    if not record:
        raise HTTPException(status_code=404, detail="прогін не знайдено")
    text = ""
    if record.report_path:
        try:
            text = Path(record.report_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = "Не вдалося прочитати файл звіту."
    elif record.rationale:
        text = record.rationale
    return views.run_detail(record, text, STATE.get("engine_warning"))


@app.get("/reports/{run_id}/raw", response_class=PlainTextResponse)
def report_raw(run_id: int, request: Request) -> str:
    store: Store = request.app.state.store
    record = store.get_run(run_id)
    if not record:
        raise HTTPException(status_code=404, detail="прогін не знайдено")
    if record.report_path:
        try:
            return Path(record.report_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return record.rationale or ""
