"""Статичний сайт результатів — те, що публікується на GitHub Pages.

Пропозиції створюємо явно (детерміновано), а не через синтетичний конвеєр:
перевіряємо саме збірку сторінок, заявку для брокера й відсутність зовнішніх
ресурсів — інакше прев'ю на GitHub Pages і в офлайн-перегляді деградує.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.db import Store
from app.proposal import Proposal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from make_site import build_site  # noqa: E402

ORDER = """КУПІВЛЯ 4 × MSFT — LIMIT 517.47 (GTC)
STOP-LOSS: 499.71 (GTC)
TAKE-PROFIT: 552.99
Якщо на відкритті ціна вище 522.64 — заявку скасувати."""


def make_proposal(**over) -> Proposal:
    base = dict(
        ticker="MSFT",
        for_session="2026-10-12",
        last_bar_date="2026-10-09",
        last_close=520.0,
        change_pct=0.6,
        bias="BUY",
        entry_type="limit",
        entry_price=517.47,
        stop_price=499.71,
        target_price=552.99,
        rr=2.0,
        qty=4,
        notional=2069.88,
        risk_usd=71.04,
        risk_pct_of_equity=0.14,
        atr_pct=2.1,
        confidence=0.87,
        approved=True,
        gate_reasons=["розмір: 4 шт. — ризик 0.14% капіталу, ліміти не перевищено"],
        reasons=["тренд висхідний", "RSI 58 у робочій зоні"],
        note="Правила: тренд угору, відкат у межах — лімітна заявка на 517.47.",
        invalidation="Денне закриття нижче 499.71 — сценарій скасовано.",
        order_template=ORDER,
        premarket_price=531.86,
        premarket_gap_pct=-0.6,
        premarket_as_of="2026-10-12T08:30-04:00",
        premarket_note="Премаркет $531.86 (-0.60%) — ґеп у межах, план лишається чинним.",
        data_source="yahoo",
        technical={"rsi14": 58.2, "atr": 11.4, "trend": "up", "signals": ["ціна над SMA50"]},
    )
    base.update(over)
    return Proposal(**base)


def _seed(settings, *proposals) -> Store:
    store = Store(settings.db_path)
    for proposal in proposals:
        store.insert_proposal(proposal)
    return store


def test_site_builds_all_pages(settings, tmp_path):
    _seed(settings, make_proposal())
    out = tmp_path / "site"
    result = build_site(settings=settings, out_dir=out, sessions=3, repo_url=None)

    assert result["session"] == "2026-10-12"
    assert result["approved"] == 1
    assert (out / "index.html").exists()
    assert (out / "results.json").exists()
    assert (out / "results.md").exists()
    assert (out / ".nojekyll").exists()

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Пропозиції на 12.10.2026 (пн)" in index
    assert "MSFT" in index
    assert "517.47" in index and "499.71" in index and "552.99" in index
    assert "Готові заявки" in index
    assert "STOP-LOSS" in index                       # текст для брокера видно на сторінці
    assert "бот лише готує план, ордери не виконуються" in index

    payload = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert payload["session"] == "2026-10-12"
    first = payload["proposals"][0]
    assert first["ticker"] == "MSFT"
    assert first["entry_price"] == 517.47
    assert first["page"] == f"proposal-{first['id']}.html"


def test_site_pages_have_no_external_resources(settings, tmp_path):
    """Сторінки мусять бути самодостатніми: без CDN, шрифтів і картинок із мережі."""
    _seed(settings, make_proposal())
    out = tmp_path / "site"
    build_site(settings=settings, out_dir=out, sessions=2, repo_url=None)

    for page in out.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert "<style>" in html
        assert "src=" not in html
        assert "http://www.w3.org/2000/svg" in html or "<svg" not in html  # xmlns — це не ресурс
        without_svg_ns = html.replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in without_svg_ns and "https://" not in without_svg_ns, page.name


def test_proposal_page_contains_plan_order_and_premarket(settings, tmp_path):
    store = _seed(settings, make_proposal())
    out = tmp_path / "site"
    build_site(settings=settings, out_dir=out, sessions=1, repo_url=None)

    proposal = store.recent_proposals(limit=1)[0]
    page = out / f"proposal-{proposal.id}.html"
    assert page.exists()

    html = page.read_text(encoding="utf-8")
    assert "MSFT" in html
    assert "Текст заявки для брокера" in html
    assert "STOP-LOSS" in html
    assert "Премаркет" in html and "ґеп у межах" in html
    assert "Вердикт ризик-гейта" in html
    assert "Паспорт плану" in html
    assert "Бот не виконує ордерів" in html


def test_wait_proposal_has_no_order_but_is_visible(settings, tmp_path):
    store = _seed(settings, make_proposal(
        ticker="AAPL", bias="WAIT", entry_price=None, stop_price=None, target_price=None,
        rr=None, qty=0, approved=False, order_template="",
        # саме так виглядає реальна WAIT-пропозиція: причина — у reasons
        reasons=["немає сетапу на найближчу сесію: імпульс негативний (MACD нижче сигнальної)"],
        gate_reasons=["немає сетапу — ризик-гейт не застосовується"]))
    out = tmp_path / "site"
    build_site(settings=settings, out_dir=out, sessions=1, repo_url=None)

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "AAPL" in index
    assert "БЕЗ СЕТАПУ" in index
    assert "Готові заявки" not in index               # жодної заявки — жодного блоку заявок
    assert "імпульс негативний" in index          # конкретна причина, а не загальне «гейт не застосовується»
    assert store.recent_proposals(limit=1)[0].ticker == "AAPL"


def test_results_markdown_has_table_and_disclaimer(settings, tmp_path):
    _seed(settings, make_proposal(), make_proposal(ticker="NVDA", bias="WAIT", qty=0,
                                                   approved=False, entry_price=None,
                                                   stop_price=None, target_price=None, rr=None))
    out = tmp_path / "site"
    build_site(settings=settings, out_dir=out, sessions=1, repo_url=None)

    markdown = (out / "results.md").read_text(encoding="utf-8")
    assert markdown.startswith("# Пропозиції на 12.10.2026 (пн)")
    assert "| Тікер |" in markdown
    assert "| MSFT | BUY |" in markdown and "| NVDA | WAIT |" in markdown
    assert "Ордери не виконуються" in markdown


def test_index_shows_schedule_and_journal(settings, tmp_path):
    settings.config["schedule"]["enabled"] = True
    _seed(settings, make_proposal())
    out = tmp_path / "site"
    build_site(settings=settings, out_dir=out, sessions=5, repo_url=None)

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "30 8 * * 1-5" in index                     # розклад видно на сторінці
    assert "Журнал прогонів" in index
    assert "12.10.2026 (пн)" in index                  # рядок журналу за сесією


def test_synthetic_data_is_flagged_on_site(settings, tmp_path):
    _seed(settings, make_proposal(synthetic=True, data_source="synthetic"))
    out = tmp_path / "site"
    build_site(settings=settings, out_dir=out, sessions=1, repo_url=None)
    assert "синтетична" in (out / "index.html").read_text(encoding="utf-8").lower()


def test_broken_proposal_does_not_brake_the_site(settings, tmp_path):
    """Пропозиція без рівнів (обірваний розрахунок) не має валити збірку."""
    _seed(settings, make_proposal(entry_price=None, stop_price=None, target_price=None, rr=None))
    out = tmp_path / "site"
    result = build_site(settings=settings, out_dir=out, sessions=1, repo_url=None)
    assert result["proposals"] == 1
    assert "—" in (out / "index.html").read_text(encoding="utf-8")


def test_site_renders_twenty_positions(settings, tmp_path):
    """Список із 20 тікерів: усі рядки мають бути на сторінці й у markdown."""
    from tests.test_site import make_proposal  # noqa: F401  (сам файл)

    tickers = ["AAPL", "AMD", "AMZN", "AVGO", "COST", "CRM", "GOOGL", "JPM", "LLY", "META",
               "MSFT", "NFLX", "NVDA", "ORCL", "QQQ", "SPY", "TSLA", "UNH", "V", "XOM"]
    store = Store(settings.db_path)
    for index, ticker in enumerate(tickers):
        store.insert_proposal(make_proposal(ticker=ticker, entry_price=100.0 + index,
                                            stop_price=95.0 + index, target_price=115.0 + index))
    out = tmp_path / "site"
    result = build_site(settings=settings, out_dir=out, sessions=1, repo_url=None)

    assert result["proposals"] == 20
    index_html = (out / "index.html").read_text(encoding="utf-8")
    for ticker in tickers:
        assert f">{ticker}<" in index_html, ticker     # є рядок у таблиці
    markdown = (out / "results.md").read_text(encoding="utf-8")
    assert sum(1 for ticker in tickers if f"| {ticker} |" in markdown) == 20
    assert len(list(out.glob("proposal-*.html"))) == 20
