"""Ціна на момент перевірки: премаркет / сесія / після закриття / остання сесія.

Головна пастка, яку ці тести і фіксують: план будується на **закритих** свічках, а
користувач дивиться на ціну **зараз** (у брокера — часто ще й овернайт). Це різні
числа за визначенням, тому в пропозиції вони тепер розведені: `last_close` і
`live_price` з підписом, що це за ціна. Мережу підмінюємо — тести офлайнові.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app import quote as quote_module
from app.db import Store
from app.market import Bar
from app.proposal import Proposal


class _Resp:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise quote_module.httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]


def yahoo_payload(points: list[tuple[str, float]], *, prev_close: float | None = 520.0,
                  regular: float | None = None) -> dict:
    """Відповідь Yahoo chart: `points` — [(ISO-мітка в ET, ціна)]."""
    stamps, closes = [], []
    for stamp, price in points:
        moment = dt.datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=quote_module.ET)
        stamps.append(int(moment.timestamp()))
        closes.append(price)
    meta = {"chartPreviousClose": prev_close, "regularMarketPrice": regular or (points[-1][1] if points else None)}
    return {"chart": {"result": [{"meta": meta, "timestamp": stamps,
                                  "indicators": {"quote": [{"close": closes}]}}]}}


# ─── визначення сесії ────────────────────────────────────────────────────────

@pytest.mark.parametrize("iso,expected", [
    ("2026-10-12T03:55", quote_module.SESSION_LAST),      # до 04:00 ET — ще немає премаркету
    ("2026-10-12T04:05", quote_module.SESSION_PRE),
    ("2026-10-12T08:42", quote_module.SESSION_PRE),
    ("2026-10-12T09:29", quote_module.SESSION_PRE),
    ("2026-10-12T09:31", quote_module.SESSION_REGULAR),
    ("2026-10-12T15:59", quote_module.SESSION_REGULAR),
    ("2026-10-12T16:05", quote_module.SESSION_AFTER),
    ("2026-10-12T19:59", quote_module.SESSION_AFTER),
    ("2026-10-12T20:30", quote_module.SESSION_LAST),
])
def test_session_of_covers_the_trading_day(iso, expected):
    moment = dt.datetime.fromisoformat(iso).replace(tzinfo=quote_module.ET)
    assert quote_module.session_of(moment) == expected


def test_winter_time_is_handled_by_the_exchange_timezone():
    """6:00 ET у січні та в липні — той самий премаркет, попри різницю в UTC."""
    winter = dt.datetime(2026, 1, 15, 11, 0, tzinfo=dt.timezone.utc)     # 06:00 EST
    summer = dt.datetime(2026, 7, 15, 10, 0, tzinfo=dt.timezone.utc)     # 06:00 EDT
    assert quote_module.session_of(winter) == quote_module.SESSION_PRE
    assert quote_module.session_of(summer) == quote_module.SESSION_PRE


# ─── розбір відповідей джерел ────────────────────────────────────────────────

def test_premarket_price_is_used_when_available(settings, monkeypatch):
    payload = yahoo_payload([("2026-10-12T08:35", 528.4), ("2026-10-12T08:41", 529.1)])
    monkeypatch.setattr(quote_module.httpx, "get", lambda *a, **k: _Resp(payload))
    quote = quote_module.live_quote("MSFT", settings, force_refresh=True)
    assert quote["price"] == 529.1
    assert quote["session"] == quote_module.SESSION_PRE
    assert quote["label"].startswith("премаркет 08:41")
    assert quote["prev_close"] == 520.0
    assert quote["source"] == "yahoo-1m"


def test_weekend_data_falls_back_to_last_session_and_is_marked_stale(settings, monkeypatch):
    """Субота: хвилинних барів за сьогодні немає — беремо meta і позначаємо, що ціна не свіжа."""
    payload = yahoo_payload([("2026-10-09T15:59", 535.07), ("2026-10-09T16:00", 535.07)],
                            regular=535.07)
    monkeypatch.setattr(quote_module.httpx, "get", lambda *a, **k: _Resp(payload))
    quote = quote_module.live_quote("MSFT", settings, force_refresh=True)
    assert quote["session"] == quote_module.SESSION_LAST
    assert quote["stale"] is True
    assert quote["session_date"] == "2026-10-09"
    assert quote["label"].startswith("закриття 09.10")     # без «19:59 ET»


def test_meta_regular_price_used_when_no_minutes(settings, monkeypatch):
    payload = {"chart": {"result": [{"meta": {"chartPreviousClose": 522.6,
                                              "regularMarketPrice": 535.07,
                                              "regularMarketTime": int(dt.datetime(
                                                  2026, 10, 9, 16, 0, tzinfo=quote_module.ET).timestamp())},
                                   "timestamp": [], "indicators": {"quote": [{"close": []}]}}]}}
    monkeypatch.setattr(quote_module.httpx, "get", lambda *a, **k: _Resp(payload))
    quote = quote_module.live_quote("MSFT", settings, force_refresh=True)
    assert quote["price"] == 535.07 and quote["source"] == "yahoo-meta"
    assert quote["stale"] is True


def test_nasdaq_is_used_when_yahoo_fails(settings, monkeypatch):
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "yahoo" in url:
            raise quote_module.httpx.ConnectError("yahoo недоступний")
        return _Resp({"data": {"primaryData": {"lastSalePrice": "$535.07",
                                               "lastTradeTimestamp": "Oct 9, 2026"}}})

    monkeypatch.setattr(quote_module.httpx, "get", fake_get)
    quote = quote_module.live_quote("MSFT", settings, force_refresh=True)
    assert quote["price"] == 535.07 and quote["source"] == "nasdaq"
    assert any("nasdaq" in url for url in calls) and any("yahoo" in url for url in calls)


def test_all_sources_failing_returns_none_without_raising(settings, monkeypatch):
    def boom(*a, **k):
        raise quote_module.httpx.ConnectError("нема мережі")

    monkeypatch.setattr(quote_module.httpx, "get", boom)
    assert quote_module.live_quote("MSFT", settings, force_refresh=True) is None


def test_quote_is_cached_for_five_minutes(settings, monkeypatch):
    calls: list[int] = []

    def fake_get(*a, **k):
        calls.append(1)
        return _Resp(yahoo_payload([("2026-10-12T08:41", 529.1)]))

    monkeypatch.setattr(quote_module.httpx, "get", fake_get)
    first = quote_module.live_quote("MSFT", settings)
    second = quote_module.live_quote("MSFT", settings)          # з кеша, без запиту
    assert first == second and len(calls) == 1
    quote_module.live_quote("MSFT", settings, force_refresh=True)
    assert len(calls) == 2

    path = settings.cache_dir / "quotes" / "MSFT.json"
    assert path.exists() and "fetched_at" in path.read_text(encoding="utf-8")


def test_stale_cache_is_ignored(tmp_path):
    """Запис, старший за TTL, не використовується (інакше показували б учорашню ціну)."""
    cache = tmp_path / "cache"
    quote_module.write_cached(cache, "MSFT", {"price": 1.0})
    path = cache / "quotes" / "MSFT.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=quote_module.QUOTE_TTL_MIN + 5)
    payload["fetched_at"] = old.isoformat(timespec="seconds")
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert quote_module.read_cached(cache, "MSFT") is None


# ─── інтеграція з пропозицією ────────────────────────────────────────────────

def _bars(closes: list[float]) -> list[Bar]:
    bars, day = [], dt.date(2026, 6, 1)
    for close in closes:
        bars.append(Bar(date=day.isoformat(), open=close, high=close * 1.01, low=close * 0.99,
                        close=close, volume=1_000_000))
        day += dt.timedelta(days=1)
    return bars


def deep_settings(tmp_path, settings):
    import copy

    from app.settings import Settings

    cfg = copy.deepcopy(settings.config)
    cfg["depth"] = {**(cfg.get("depth") or {}), "enabled": False, "live_quote": True}
    return Settings(config=cfg, base_dir=tmp_path, data_dir=tmp_path, db_path=tmp_path / "assistant.db",
                    reports_dir=tmp_path / "reports", outbox_path=tmp_path / "outbox.log",
                    cache_dir=tmp_path / "cache", env={})


def test_wait_proposal_also_gets_live_price(settings, monkeypatch):
    """Навіть коли угоди немає, ціна на момент перевірки мусить стояти в рядку."""
    from app import market as market_module
    from app import proposal as proposal_module

    tests_settings = deep_settings(settings.base_dir, settings)
    bars = _bars([100 + i * 0.5 for i in range(80)])
    monkeypatch.setattr(market_module, "fetch_daily",
                        lambda ticker, settings, force_refresh=False: type("D", (), {
                            "ticker": ticker, "bars": bars, "source": "test", "synthetic": False,
                            "warnings": []})())
    monkeypatch.setattr(quote_module.httpx, "get",
                        lambda *a, **k: _Resp(yahoo_payload([("2026-10-12T08:41", 149.9)])))

    store = Store(tests_settings.db_path)
    store.seed_watchlist(["NVDA"])
    proposal = proposal_module.build_proposal(tests_settings, store, "NVDA")

    assert proposal.live_price == 149.9
    assert proposal.live_session == quote_module.SESSION_PRE
    assert proposal.live_vs_close_pct is not None          # рух від закриття — теж показуємо
    assert proposal.live_source == "yahoo-1m"


def test_missing_quote_adds_warning_and_does_not_break_plan(settings, monkeypatch):
    from app import market as market_module
    from app import proposal as proposal_module

    tests_settings = deep_settings(settings.base_dir, settings)
    bars = _bars([100 + i * 0.5 for i in range(80)])
    monkeypatch.setattr(market_module, "fetch_daily",
                        lambda ticker, settings, force_refresh=False: type("D", (), {
                            "ticker": ticker, "bars": bars, "source": "test", "synthetic": False,
                            "warnings": []})())

    def boom(*a, **k):
        raise quote_module.httpx.ConnectError("offline")

    monkeypatch.setattr(quote_module.httpx, "get", boom)
    store = Store(tests_settings.db_path)
    proposal = proposal_module.build_proposal(tests_settings, store, "NVDA")
    assert proposal.live_price is None
    assert any("ціну на момент перевірки" in w for w in proposal.warnings)


def test_live_price_is_stored_and_read_back(settings, monkeypatch):
    """Поле має пережити запис у БД — інакше на сторінці його не буде."""
    from app import market as market_module
    from app import proposal as proposal_module

    tests_settings = deep_settings(settings.base_dir, settings)
    bars = _bars([100 + i * 0.5 for i in range(80)])
    monkeypatch.setattr(market_module, "fetch_daily",
                        lambda ticker, settings, force_refresh=False: type("D", (), {
                            "ticker": ticker, "bars": bars, "source": "test", "synthetic": False,
                            "warnings": []})())
    monkeypatch.setattr(quote_module.httpx, "get",
                        lambda *a, **k: _Resp(yahoo_payload([("2026-10-12T08:41", 151.25)])))
    store = Store(tests_settings.db_path)
    made = proposal_module.build_proposal(tests_settings, store, "NVDA")

    saved = [p for p in store.proposals_for_session(made.for_session) if p.ticker == "NVDA"][-1]
    assert saved.live_price == 151.25
    assert saved.live_label.startswith("премаркет")
    assert saved.live_source == "yahoo-1m"


# ─── показ на сторінці результатів ───────────────────────────────────────────

def test_site_shows_price_at_check_time_for_every_row(settings, monkeypatch, tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from make_site import build_site

    store = Store(settings.db_path)
    settings.config["watchlist"] = ["MSFT", "NVDA"]

    def make(ticker, bias, price, approved):
        p = Proposal(ticker=ticker, for_session="2026-10-12", last_bar_date="2026-10-09",
                     last_close=520.0, change_pct=0.6, bias=bias, approved=approved,
                     reasons=["немає сетапу на найближчу сесію: імпульс негативний"],
                     live_price=price, live_label="премаркет 08:41 ET",
                     live_session="премаркет", live_as_of="2026-10-12T08:41",
                     live_source="yahoo-1m", live_vs_close_pct=1.75)
        p.id = store.insert_proposal(p)
        return p

    make("MSFT", "BUY", 529.1, True)
    make("NVDA", "WAIT", 231.4, False)

    out_dir = tmp_path / "site"
    build_site(settings=settings, out_dir=out_dir, sessions=5, pages_for_sessions=1)
    index = (out_dir / "index.html").read_text(encoding="utf-8")
    payload = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    markdown = (out_dir / "results.md").read_text(encoding="utf-8")

    assert "Ціна зараз" in index
    assert "529.1" in index and "231.4" in index
    assert index.count("премаркет 08:41 ET") >= 2                    # обидва рядки, включно з WAIT
    assert "цим і є умова заявки" not in index
    assert "це і є умова заявки" in index
    assert "| MSFT | 529.10 (премаркет 08:41 ET) | BUY |" in markdown
    assert "| NVDA | 231.40 (премаркет 08:41 ET) | WAIT |" in markdown   # ціна є навіть без сетапу
    values = {p["ticker"]: p for p in payload["proposals"]}
    assert values["MSFT"]["live_price"] == 529.1
    assert values["NVDA"]["live_session"] == "премаркет"


def test_closed_market_prefers_official_close_over_after_hours_print(settings, monkeypatch):
    """Після закриття остання хвилина — це принт 19:59 ET, а брокер показує закриття 16:00.
    Якщо взяти принт, користувач бачить «розбіжність» у 0.05–0.2% і не розуміє, чому."""
    payload = yahoo_payload([("2026-10-09T15:59", 535.05), ("2026-10-09T19:59", 535.00)],
                            regular=535.07)
    monkeypatch.setattr(quote_module.httpx, "get", lambda *a, **k: _Resp(payload))
    quote = quote_module.live_quote("MSFT", settings, force_refresh=True)
    assert quote["price"] == 535.07                      # офіційне закриття, як у брокера
    assert quote["source"] == "yahoo-meta"
    assert quote["label"] == "закриття 09.10"
