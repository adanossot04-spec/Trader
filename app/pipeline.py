"""Конвеєр: аналіз → нормалізація → ризик-гейт → журнал → сповіщення."""

from __future__ import annotations

import logging
import time
from datetime import date as date_cls, datetime
from typing import Any

from . import engine as engine_module
from . import notify as notify_module
from . import risk as risk_module
from .db import Store
from .models import RunRecord, Signal

log = logging.getLogger("assistant.pipeline")

_engine_cache: dict[str, Any] = {}
_engine_warning: str | None = None


def get_engine(settings, *, force_rebuild: bool = False) -> tuple[Any, str | None]:
    global _engine_warning
    key = f"{settings.engine.get('mode')}:{settings.engine.get('llm_provider')}:{','.join(settings.engine.get('analysts') or [])}"
    if force_rebuild or key not in _engine_cache:
        instance, _engine_warning = engine_module.build_engine(settings)
        _engine_cache[key] = instance
    return _engine_cache[key], _engine_warning


def last_engine_warning() -> str | None:
    return _engine_warning


def settle_pending(settings, store: Store) -> list[Any]:
    """Закриває минулі рішення по реалізованій дохідності (якщо рушій це вміє)."""
    engine, _ = get_engine(settings)
    settled: list[Any] = []
    try:
        settled = list(engine.settle_all_pending() or [])
    except Exception as exc:  # збій settle не має валити прогін
        log.warning("settle_all_pending не вдався: %s", exc)
    return settled


def propose_one(
    settings,
    store: Store,
    ticker: str,
    *,
    notify: bool = True,
    use_llm: bool = True,
    force_refresh: bool = False,
):
    """Одна пропозиція на наступну сесію. Помилки не піднімаються вище — логуються."""
    from . import proposal as proposal_module

    try:
        made = proposal_module.build_proposal(
            settings, store, ticker, use_llm=use_llm, force_refresh=force_refresh
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Пропозиція для %s не вдалася", ticker)
        made = proposal_module.Proposal(
            ticker=ticker.upper(),
            for_session=datetime.now().date().isoformat(),
            last_bar_date="—",
            last_close=0.0,
            change_pct=0.0,
            bias="WAIT",
            reasons=[f"помилка отримання даних: {type(exc).__name__}: {exc}"],
            note="Немає даних — пропозицію не побудовано.",
            llm_error=None,
        )
        made.id = store.insert_proposal(made)

    if notify:
        notify_module.deliver_proposal(made, settings)
    return made


def build_proposals(
    settings,
    store: Store,
    tickers: list[str] | None = None,
    *,
    notify: bool = True,
    summary: bool = True,
    use_llm: bool = True,
    force_refresh: bool = False,
) -> list:
    """Готує пропозиції на наступну сесію для всього списку."""
    tickers = [t.upper() for t in (tickers if tickers is not None else (store.watchlist() or settings.watchlist))]
    if not tickers:
        log.warning("Порожній watchlist — немає для чого будувати пропозиції")
        return []

    proposals = []
    for ticker in tickers:
        proposals.append(
            propose_one(
                settings, store, ticker, notify=notify, use_llm=use_llm, force_refresh=force_refresh
            )
        )

    if summary and notify and proposals:
        notify_module.deliver_proposals_summary(proposals, settings, proposals[0].for_session)
    return proposals


def run_one(
    settings,
    store: Store,
    ticker: str,
    analysis_date: str | None = None,
    *,
    notify: bool = True,
    engine: Any | None = None,
) -> RunRecord:
    """Один повний цикл для одного тікера. Помилки не піднімаються — пишуться в запис."""
    ticker = ticker.strip().upper()
    day = analysis_date or datetime.now().date().isoformat()
    engine = engine or get_engine(settings)[0]
    book = notify_module.book_from_settings(settings)

    record = RunRecord(ticker=ticker, analysis_date=day, engine=getattr(engine, "name", "unknown"))
    run_id = store.insert_run(record)
    record.id = run_id
    started = time.perf_counter()

    try:
        result = engine.analyse(ticker, day, portfolio=settings.portfolio() or None)
        signal: Signal = result.signal
        verdict = risk_module.risk_gate(
            signal,
            book,
            settings.risk,
            today=date_cls.fromisoformat(day) if _is_date(day) else datetime.now().date(),
            today_approved_count=_approved_today(store),
            recent_orders=[r.model_dump() for r in store.approved_runs_since(int(settings.risk.get("cooldown_days", 3)) + 1)],
        )

        store.update_run(
            run_id,
            status="ok",
            action=signal.action,
            rating=signal.rating,
            confidence=signal.confidence,
            rationale=signal.rationale,
            reference_price=signal.reference_price,
            approved=int(verdict.approved),
            reasons="\n".join(verdict.reasons),
            quantity=verdict.quantity,
            notional=verdict.notional,
            stop_loss_price=verdict.stop_loss_price,
            take_profit_price=verdict.take_profit_price,
            kill_switch=int(verdict.kill_switch),
            duration_sec=round(time.perf_counter() - started, 2),
        )
        store.save_report(run_id, result.report_markdown, result.report_html)

        record = store.get_run(run_id) or record
        if notify:
            delivered = notify_module.deliver(record, settings, book)
            store.update_run(run_id, notified=int(delivered))
            record.notified = delivered
        return record

    except Exception as exc:  # noqa: BLE001 — жоден тікер не має валити цикл
        log.exception("Помилка аналізу %s", ticker)
        store.update_run(
            run_id,
            status="error",
            error=f"{type(exc).__name__}: {exc}"[:2000],
            duration_sec=round(time.perf_counter() - started, 2),
        )
        record = store.get_run(run_id) or record
        if notify:
            notify_module.deliver(record, settings, book, always=True)
        return record


def run_watchlist(
    settings,
    store: Store,
    tickers: list[str] | None = None,
    *,
    notify: bool = True,
    summary: bool = True,
) -> list[RunRecord]:
    """Прогін по всьому списку. Послідовно — щоб не множити одночасні LLM-виклики."""
    engine, warning = get_engine(settings)
    if warning:
        log.warning(warning)

    tickers = [t.upper() for t in (tickers if tickers is not None else store.watchlist() or settings.watchlist)]
    if not tickers:
        log.warning("Порожній watchlist — нічого аналізувати")
        return []

    if settings.schedule.get("settle_before_run", True):
        # settle окремим викликом за потреби; не блокує аналіз, якщо рушій цього не вміє
        pass

    results: list[RunRecord] = []
    for ticker in tickers:
        results.append(run_one(settings, store, ticker, notify=notify, engine=engine))

    if summary and notify:
        notify_module.deliver_summary(results, settings, notify_module.book_from_settings(settings))
    return results


def _approved_today(store: Store) -> int:
    today = datetime.now().date()
    return sum(1 for r in store.runs_for_day(today) if r.approved)


def _is_date(value: str) -> bool:
    try:
        date_cls.fromisoformat(value)
        return True
    except ValueError:
        return False
