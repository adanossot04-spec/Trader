"""Журнал і рендеринг сторінок — щоб дашборд не падав на реальних даних."""

from __future__ import annotations

from app import views
from app.db import Store
from app.models import RunRecord
from app.risk import Book


def make_record(**over) -> RunRecord:
    base = dict(
        ticker="NVDA",
        analysis_date="2026-10-10",
        engine="mock",
        status="ok",
        action="BUY",
        rating="Buy",
        confidence=0.72,
        rationale="текст із <html> та & амперсандом",
        reference_price=182.4,
        approved=True,
        reasons="розмір: 1 шт.",
        quantity=1,
        notional=182.4,
        stop_loss_price=167.81,
        take_profit_price=218.88,
    )
    base.update(over)
    return RunRecord(**base)


def test_store_roundtrip(settings):
    store = Store(settings.db_path)
    run_id = store.insert_run(make_record())
    store.update_run(run_id, approved=1, notified=0)

    record = store.get_run(run_id)
    assert record is not None and record.approved is True
    assert store.stats()["approved"] == 1
    assert [r.id for r in store.recent_runs()] == [run_id]


def test_watchlist_crud(settings):
    store = Store(settings.db_path)
    store.seed_watchlist(["NVDA", "nvda", "AAPL"])
    assert store.watchlist() == ["AAPL", "NVDA"]     # дедуплікація + нормалізація регістру
    store.add_ticker("tsla")
    assert "TSLA" in store.watchlist()
    store.remove_ticker("AAPL")
    assert "AAPL" not in store.watchlist()


def test_save_report_writes_files(settings):
    store = Store(settings.db_path)
    run_id = store.insert_run(make_record())
    path = store.save_report(run_id, "# звіт", "<html>звіт</html>")
    assert path.exists()
    record = store.get_run(run_id)
    assert record.report_path == str(path)
    assert record.report_html and str(record.report_html).endswith("report.html")


def test_dashboard_renders_with_data(settings):
    store = Store(settings.db_path)
    store.seed_watchlist(["NVDA"])
    store.insert_run(make_record())
    stats = store.stats() | store.proposal_stats() | {"watchlist": store.watchlist()}
    html = views.dashboard([], store.recent_runs(), stats, settings, {"engine": "mock", "running": False}, None)
    assert "Trading Assistant" in html
    assert "NVDA" in html


def test_dashboard_renders_proposal(settings):
    from app.proposal import Proposal

    store = Store(settings.db_path)
    proposal = Proposal(
        ticker="NVDA", for_session="2026-10-12", last_bar_date="2026-10-09", last_close=229.28, change_pct=0.5,
        bias="BUY", entry_type="limit", entry_price=228.0, stop_price=220.1, target_price=245.0, rr=2.1,
        qty=3, notional=684.0, risk_usd=24.0, risk_pct_of_equity=0.05, confidence=0.68, approved=True,
        gate_reasons=["розмір: 3 шт."], reasons=["тренд висхідний"], note="План: ліміт 228.00",
    )
    store.insert_proposal(proposal)
    proposals = store.proposals_for_session("2026-10-12")
    stats = store.stats() | store.proposal_stats() | {"watchlist": store.watchlist()}
    html = views.dashboard(proposals, [], stats, settings, {"engine": "mock", "running": False}, None)
    assert "NVDA" in html and "Пропозиції на" in html and "228.00" in html


def test_run_detail_and_settings_pages_render(settings):
    record = make_record()
    page = views.run_detail(record, "текст звіту", None)
    assert "Звіт" in page and "KILL-SWITCH" not in page

    page2 = views.settings_page(
        settings, {"runs_total": 1, "runs_ok": 1, "runs_error": 0, "approved": 1, "settled": 0,
                   "proposals_total": 3, "proposals_approved": 1, "proposals_wait": 2},
        {"telegram_token": False, "telegram_chat": False}, "{}", None,
        llm_status={"provider": "none", "model": None, "key_present": True, "hint": "LLM вимкнено"},
        data_sources=["yahoo", "twelvedata"],
    )
    assert "Налаштування" in page2


def test_dashboard_shows_schedule_and_next_run(settings):
    store = Store(settings.db_path)
    settings.config["schedule"]["enabled"] = True
    stats = store.stats() | store.proposal_stats() | {"watchlist": store.watchlist()}
    html = views.dashboard([], [], stats, settings, {"engine": "mock", "next_run": "12.10 15:30 EEST (pre_open)"}, None)
    assert "America/New_York" in html
    assert "12.10 15:30" in html          # час наступного запуску
    assert "30 8" in html or "8:30" in html


def test_proposal_page_shows_premarket_card(settings):
    from app.proposal import Proposal

    store = Store(settings.db_path)
    proposal = Proposal(
        ticker="NVDA", for_session="2026-10-12", last_bar_date="2026-10-09", last_close=210.8, change_pct=-1.0,
        bias="BUY", entry_type="limit", entry_price=204.22, stop_price=199.08, target_price=215.0, rr=2.1,
        qty=12, notional=2450.64, risk_usd=61.68, risk_pct_of_equity=0.12, confidence=0.8, approved=True,
        premarket_price=211.64, premarket_gap_pct=0.4, premarket_as_of="2026-10-12T08:30-04:00",
        premarket_note="Премаркет $211.64 (+0.40%) — ґеп у межах, план лишається чинним.",
        reasons=["тренд висхідний"], gate_reasons=["розмір: 12 шт."], note="План: ліміт 204.22",
    )
    store.insert_proposal(proposal)
    saved = store.proposals_for_session("2026-10-12")[0]

    html = views.proposal_detail(saved, "<svg></svg>", settings, None)
    assert "Премаркет" in html
    assert "211.64" in html and "+0.40%" in html
    assert "09:30 ET" in html            # рядок часу відкриття


def test_pages_survive_proposal_without_levels(settings):
    """BUY без рівнів (напр. обірваний розрахунок) не має валити дашборд і сторінку."""
    from app.proposal import Proposal

    store = Store(settings.db_path)
    broken = Proposal(
        ticker="BRK", for_session="2026-10-12", last_bar_date="2026-10-09", last_close=None, change_pct=None,
        bias="BUY", entry_price=None, stop_price=None, target_price=None, rr=None, qty=0, approved=False,
        confidence=None, reasons=["розрахунок обірвано"], gate_reasons=["немає даних"],
    )
    store.insert_proposal(broken)
    saved = store.proposals_for_session("2026-10-12")[0]
    stats = store.stats() | store.proposal_stats() | {"watchlist": store.watchlist()}

    html = views.dashboard([saved], [], stats, settings, {"engine": "mock"}, None)
    assert "BRK" in html and "—" in html
    assert "BRK" in views.proposal_detail(saved, "<svg></svg>", settings, None)


def test_book_helper(settings):
    book = Book.from_dict({"cash": 1000, "positions": [{"ticker": "NVDA", "quantity": 2, "average_price": 100}]})
    assert book.equity == 1200
    assert book.position("nvda") is not None
