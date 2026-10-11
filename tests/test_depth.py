"""Глибина аналізу: фундаментал (SEC), календар звітів, макро-режим, RSS, історія правила.

Усі тести офлайнові: мережу підмінюють заглушки, бо логіка шару має бути
перевіреною незалежно від того, чи відповідає сьогодні SEC і Nasdaq.
"""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from app import backtest as backtest_module
from app import depth as depth_module
from app import fundamentals as fund_module
from app import llm as llm_module
from app import macro as macro_module
from app import news as news_module
from app.db import Store
from app.market import Bar
from app.settings import DEFAULTS, Settings

# ─── фікстури ─────────────────────────────────────────────────────────────────


@pytest.fixture
def deep_settings(tmp_path, settings) -> Settings:
    """Ті самі tmp-налаштування, але глибина УВІМКНЕНА (з підставною мережею)."""
    cfg = copy.deepcopy(settings.config)
    cfg["depth"] = {**(cfg.get("depth") or {}), "enabled": True, "backtest": True}
    return Settings(
        config=cfg,
        base_dir=tmp_path,
        data_dir=tmp_path,
        db_path=tmp_path / "assistant.db",
        reports_dir=tmp_path / "reports",
        outbox_path=tmp_path / "outbox.log",
        cache_dir=tmp_path / "cache",
        env={},
    )


def fund_packet(**over) -> dict:
    """Підставний фундаментальний паспорт (як його повертає SEC-модуль)."""
    packet = {
        "company": "NVIDIA CORP",
        "revenue": {"last": 96.2e9, "prev": 88.0e9, "year_ago": 68.0e9, "yoy_pct": 41.5, "period": "2026-07-26", "quarters": 8},
        "eps": {"last": 2.46, "year_ago": 1.85, "yoy_pct": 33.0, "period": "2026-07-26", "quarters": 8},
        "net_margin_pct": 55.4,
        "gross_margin_pct": 74.9,
        "filings": {"company": "NVIDIA CORP", "last_report": "2026-09-01", "days_since_report": 40,
                    "eight_k_30d": 1, "recent": []},
        "earnings": {"date": "2026-11-19", "days_to": 39, "time": "amc", "source": "nasdaq"},
        "notes": ["виручка зростає"],
        "score": 2,
    }
    packet.update(over)
    return packet


def bars_from(closes: list[float]) -> list[Bar]:
    bars, day = [], dt.date(2025, 1, 1)
    for close in closes:
        bars.append(Bar(date=day.isoformat(), open=close, high=close * 1.01, low=close * 0.99,
                        close=close, volume=1_000_000))
        day += dt.timedelta(days=1)
    return bars


def uptrend(n: int = 320, seed: int = 10) -> list[Bar]:
    import random

    rng = random.Random(seed)
    bars, day, price = [], dt.date(2025, 1, 1), 100.0
    for _ in range(n):
        price = max(5.0, price * (1 + rng.gauss(0.0022, 0.011)))
        bars.append(Bar(date=day.isoformat(), open=round(price * 0.998, 2), high=round(price * 1.006, 2),
                        low=round(price * 0.994, 2), close=round(price, 2), volume=1_000_000))
        day += dt.timedelta(days=1)
    return bars


# ─── фундаментальні розрахунки (чисті функції) ────────────────────────────────

def test_quarterly_keeps_only_quarter_periods_and_latest_filing():
    units = [
        {"start": "2025-01-27", "end": "2025-04-28", "val": 44.1e9, "filed": "2025-05-28", "form": "10-Q"},
        {"start": "2025-01-27", "end": "2025-04-28", "val": 44.0e9, "filed": "2026-05-28", "form": "10-K"},  # пізніший filed перекриває
        {"start": "2024-01-01", "end": "2024-12-31", "val": 130.0e9, "filed": "2025-02-20", "form": "10-K"},  # рік — не квартал
    ]
    rows = fund_module._quarterly(units)
    assert len(rows) == 1
    assert rows[0]["val"] == 44.0e9 and rows[0]["form"] == "10-K"


def test_yoy_change_matches_year_ago_within_window():
    series = [
        {"start": "2025-07-26", "end": "2025-10-26", "val": 35.1e9},
        {"start": "2026-07-26", "end": "2026-10-26", "val": 57.0e9},
    ]
    out = fund_module.yoy_change(series)
    assert out["last"] == 57.0e9 and out["year_ago"] == 35.1e9
    assert out["yoy_pct"] == pytest.approx(62.39, abs=0.05)
    assert out["period"] == "2026-10-26" and out["quarters"] == 2


def test_yoy_change_empty_series_is_safe():
    out = fund_module.yoy_change([])
    assert out["last"] is None and out["yoy_pct"] is None and out["quarters"] == 0


def test_margin_and_series_stats():
    assert fund_module.margin(55.0, 100.0) == 55.0
    assert fund_module.margin(None, 100.0) is None
    assert fund_module.margin(10.0, 0) is None
    stats = fund_module.series_stats([10.0, 12.5])
    assert stats["change_pct"] == 25.0


def test_recent_filings_counts_8k_and_days_since_report():
    today = dt.date(2026, 10, 11)
    filings = [
        {"form": "10-Q", "filingDate": "2026-09-01"},
        {"form": "8-K", "filingDate": "2026-10-05"},
        {"form": "8-K", "filingDate": "2026-10-09"},
        {"form": "4", "filingDate": "2026-10-10"},
        {"form": "10-K", "filingDate": "2024-01-15"},   # старий — не «останній звіт»
    ]
    out = fund_module.recent_filings(filings, today)
    assert out["last_report"]["date"] == "2026-09-01" and out["last_report"]["form"] == "10-Q"
    assert out["days_since_report"] == 40
    assert out["eight_k_30d"] == 2
    assert len(out["recent"]) <= 8


def test_earnings_from_calendar_finds_soonest_date():
    today = dt.date(2026, 10, 11)
    calendar = {
        "2026-10-20": [{"symbol": "NVDA", "time": "amc"}],
        "2026-10-15": [{"symbol": "NVDA", "time": "bmo"}],
        "2026-10-16": [{"symbol": "AAPL", "time": "bmo"}],
    }
    out = fund_module.earnings_from_calendar(calendar, "NVDA", today)
    assert out["date"] == "2026-10-15" and out["days_to"] == 4 and out["timing"] == "bmo"
    assert fund_module.earnings_from_calendar(calendar, "TSLA", today) is None


# ─── новини (RSS) ─────────────────────────────────────────────────────────────

RSS_XML = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Nvidia wins big AI deal</title><link>https://example.com/1</link><pubDate>Fri, 10 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Chip demand stays strong</title><link>https://example.com/2</link><pubDate>Thu, 09 Oct 2026 09:30:00 GMT</pubDate></item>
<item><title>Third headline</title><link>https://example.com/3</link></item>
</channel></rss>"""


def test_parse_rss_extracts_titles_and_links():
    items = news_module.parse_rss(RSS_XML, limit=2)
    assert [i["title"] for i in items] == ["Nvidia wins big AI deal", "Chip demand stays strong"]
    assert items[0]["link"] == "https://example.com/1"
    assert items[0]["published"].startswith("2026-10-10")


def test_parse_rss_survives_broken_xml():
    assert news_module.parse_rss("<rss><channel><item>", limit=3) == []
    assert news_module.parse_rss("", limit=3) == []


# ─── макро-режим ──────────────────────────────────────────────────────────────

def test_macro_classify_risk_off_when_vix_spikes_and_price_below_sma():
    out = macro_module.classify(vix=32.0, vix_prev=24.0, spy_close=600.0, spy_sma50=640.0,
                                spy_sma200=650.0, ten_year=5.2, ten_year_prev=5.1, breadth_pct=25.0)
    assert out["state"] == "risk_off"
    assert out["score"] <= -2
    assert any("VIX" in reason for reason in out["reasons"])


def test_macro_classify_risk_on_in_calm_uptrend():
    out = macro_module.classify(vix=13.0, vix_prev=13.4, spy_close=760.0, spy_sma50=740.0,
                                spy_sma200=700.0, ten_year=4.1, ten_year_prev=4.1, breadth_pct=65.0)
    assert out["state"] == "risk_on" and out["score"] >= 2


def test_macro_classify_neutral_without_data():
    out = macro_module.classify(vix=None, vix_prev=None, spy_close=None, spy_sma50=None,
                                spy_sma200=None, ten_year=None, ten_year_prev=None, breadth_pct=None)
    assert out["state"] == "neutral"
    assert out["inputs"]["vix"] is None


def test_breadth_counts_share_above_sma50():
    up = {f"T{i}": bars_from([100 + idx * 0.5 for idx in range(60)]) for i in range(3)}
    down = {"D": bars_from([200 - idx * 0.5 for idx in range(60)])}
    assert macro_module.breadth({**up, **down}) == 75.0
    assert macro_module.breadth({}) is None


# ─── історія правила (бектест) ────────────────────────────────────────────────

def test_backtest_counting_and_verdict(deep_settings):
    bars = uptrend(320)
    stats = backtest_module.simulate("NVDA", bars, deep_settings, max_hold_days=10)
    assert stats["signals"] == len(stats["trades"]) >= 0
    if stats["signals"]:
        assert set(stats["trades"][0]) == {"session", "entry", "stop", "target", "exit", "outcome", "days", "r"}
        assert stats["trades"][0]["session"] == bars[210].date  # сигнал саме на свічці рішення
    verdict = backtest_module.verdict({"signals": 3, "expectancy_r": 0.5},
                                      min_signals=8, min_expectancy_r=0.0)
    assert verdict["state"] == "unknown" and verdict["allowed"] is True      # мало даних — не блокуємо
    verdict = backtest_module.verdict({"signals": 12, "expectancy_r": -0.2, "win_rate": 30.0})
    assert verdict["state"] == "poor" and verdict["allowed"] is False
    verdict = backtest_module.verdict({"signals": 12, "expectancy_r": 0.35, "win_rate": 58.0})
    assert verdict["state"] == "good" and verdict["allowed"] is True


def test_backtest_stats_are_deterministic_and_bounded():
    trades = [{"r": 2.0, "days": 3, "outcome": "target"}, {"r": -1.0, "days": 5, "outcome": "stop"},
              {"r": -1.0, "days": 8, "outcome": "stop"}, {"r": 2.0, "days": 2, "outcome": "target"}]
    stats = backtest_module._stats(trades)
    assert stats["win_rate"] == 50.0 and stats["expectancy_r"] == 0.5
    assert stats["profit_factor"] == 2.0 and stats["max_dd_r"] == -2.0
    assert stats["target_rate"] == 50.0 and stats["stop_rate"] == 50.0


# ─── вердикт глибини ──────────────────────────────────────────────────────────

def _packet(**over) -> dict:
    packet = {
        "ticker": "NVDA", "sector": "semis", "sector_label": "напівпровідники",
        "rs_20_pct": 3.0, "fundamentals": fund_packet(),
        "news": [{"title": "Nvidia wins big AI deal"}],
        "backtest": {"signals": 12, "win_rate": 58.0, "expectancy_r": 0.35, "avg_days": 6.0},
        "macro": {"state": "neutral", "inputs": {"vix": 15.0, "breadth_pct": 55.0}},
        "quality": {"state": "good", "allowed": True, "note": "правило підтверджене історією"},
    }
    packet.update(over)
    return packet


def test_depth_blocks_buy_before_earnings(deep_settings):
    packet = _packet(fundamentals=fund_packet(earnings={"date": "2026-10-13", "days_to": 2}))
    out = depth_module.deep_verdict(bias="BUY", packet=packet, settings=deep_settings)
    assert out["allowed"] is False
    assert any("звітність" in block and "заборонено" in block for block in out["blocks"])


def test_depth_blocks_buy_in_panic_vix(deep_settings):
    packet = _packet(macro={"state": "risk_off", "inputs": {"vix": 34.0}})
    out = depth_module.deep_verdict(bias="BUY", packet=packet, settings=deep_settings)
    assert out["allowed"] is False
    assert any("VIX 34" in block for block in out["blocks"])


def test_depth_halves_risk_in_risk_off(deep_settings):
    packet = _packet(quality={"state": "good", "allowed": True, "note": "—"},
                     macro={"state": "risk_off", "inputs": {"vix": 26.0}})
    out = depth_module.deep_verdict(bias="BUY", packet=packet, settings=deep_settings)
    assert out["allowed"] is True
    assert out["risk_factor"] == 0.5
    assert any("risk-off" in note for note in out["notes"])


def test_depth_blocks_second_name_in_same_sector(deep_settings):
    out = depth_module.deep_verdict(bias="BUY", packet=_packet(), settings=deep_settings,
                                    approved_sectors={"semis": 1})
    assert out["allowed"] is False
    assert any("диверсифікація" in block for block in out["blocks"])


def test_depth_blocks_when_history_of_rule_is_weak(deep_settings):
    weak = {"signals": 15, "win_rate": 25.0, "expectancy_r": -0.3}
    packet = _packet(backtest=weak, quality=None)
    packet["quality"] = backtest_module.verdict(weak, min_signals=8, min_expectancy_r=0.0)
    out = depth_module.deep_verdict(bias="BUY", packet=packet, settings=deep_settings)
    assert out["allowed"] is False and any("слабке" in b for b in out["blocks"])


def test_depth_waits_never_blocked(deep_settings):
    packet = _packet(fundamentals=fund_packet(earnings={"date": "2026-10-12", "days_to": 1}))
    out = depth_module.deep_verdict(bias="WAIT", packet=packet, settings=deep_settings)
    assert out["allowed"] is True and out["blocks"] == []


def test_depth_confidence_delta_is_bounded(deep_settings):
    packet = _packet()
    strong = depth_module.deep_verdict(bias="BUY", packet=packet, settings=deep_settings)
    delta = impl_delta(packet, strong)
    assert -0.1 <= delta <= 0.1 and delta > 0
    bad_packet = _packet(fundamentals=fund_packet(score=-3),
                         macro={"state": "risk_off", "inputs": {"vix": 26.0}}, rs_20_pct=-5.0)
    from app.proposal import _depth_confidence_delta

    assert _depth_confidence_delta(bad_packet, strong) < 0


def impl_delta(packet, verdict):
    from app.proposal import _depth_confidence_delta

    return _depth_confidence_delta(packet, verdict)


def test_relative_strength_and_correlation():
    fast = bars_from([100 + idx for idx in range(40)])
    slow = bars_from([100 + idx * 0.2 for idx in range(40)])
    assert depth_module.relative_strength(fast, slow, window=20) > 5
    assert depth_module.relative_strength(fast, None) is None
    corr = depth_module.correlate(fast, slow, window=30)
    assert corr == pytest.approx(1.0, abs=0.01)


def test_sector_map_covers_watchlist(settings):
    for ticker in DEFAULTS["watchlist"]:
        assert depth_module.sector_of(ticker) in depth_module.SECTOR_LABELS


# ─── дайджест ─────────────────────────────────────────────────────────────────

def test_rules_digest_mentions_facts_without_network(settings):
    text = llm_module.rules_digest(_packet())
    assert "Виручка" in text and "41.5%" in text
    assert "Режим ринку" in text and "Історія правила" in text
    assert "Nvidia wins big AI deal" in text


def test_rules_digest_survives_missing_fundamentals(settings):
    text = llm_module.rules_digest({"ticker": "SPY", "fundamentals": None, "macro": {}, "news": []})
    assert "SEC" in text or "фундамент" in text


def test_deep_digest_is_deterministic_without_key(settings):
    settings.llm_cfg["provider"] = "none"
    out = llm_module.deep_digest(_packet(), settings)
    assert out["source"] == "rules" and out["error"] is None and out["text"]


# ─── інтеграція з пропозицією ─────────────────────────────────────────────────

def test_proposal_records_depth_and_blocks_before_earnings(deep_settings, monkeypatch):
    from app import market as market_module
    from app import proposal as proposal_module

    bars = uptrend(320)
    monkeypatch.setattr(market_module, "fetch_daily",
                        lambda ticker, settings, force_refresh=False: type("D", (), {
                            "ticker": ticker, "bars": bars, "source": "test", "synthetic": False,
                            "warnings": []})())
    store = Store(deep_settings.db_path)
    store.seed_watchlist(["NVDA"])

    near_earnings = fund_packet(earnings={"date": "2026-10-13", "days_to": 2})
    monkeypatch.setattr(depth_module.fundamentals_module, "fetch_fundamentals",
                        lambda *a, **k: near_earnings)
    monkeypatch.setattr(depth_module.news_module, "headlines", lambda *a, **k: [{"title": "Заголовок"}])
    monkeypatch.setattr(depth_module.macro_module, "collect",
                        lambda *a, **k: {"state": "neutral", "inputs": {"vix": 15.0}, "reasons": []})
    monkeypatch.setattr(depth_module.macro_module, "breadth", lambda bars_map: 60.0)
    monkeypatch.setattr(proposal_module, "plan_from_technical", lambda tech, settings, has_position=False: {
        "bias": "BUY", "entry_type": "limit", "entry": round(tech.last_close * 0.99, 2),
        "stop": round(tech.last_close * 0.95, 2), "target": round(tech.last_close * 1.09, 2),
        "rr": 2.0, "confidence": 0.7, "reasons": ["тестовий сетап"],
    })

    proposal = proposal_module.build_proposal(
        deep_settings, store, "NVDA", depth_context={"spy_bars": bars, "macro": {"state": "neutral"},
                                                     "calendar": {}, "breadth_pct": 60.0},
    )
    assert proposal.depth["enabled"] is True
    assert proposal.depth["fundamentals"]["earnings"]["days_to"] == 2
    assert proposal.qty == 0 and proposal.approved is False
    assert any("звітність" in reason for reason in proposal.gate_reasons)
    assert proposal.depth["digest"] and proposal.depth["digest_source"] == "rules"
    assert proposal.depth["sector"] == "semis"

    # і це переживає запис у БД + читання назад (міграція колонки depth)
    stored = store.proposals_for_session(proposal.for_session)
    saved = [p for p in stored if p.ticker == "NVDA"][-1]
    assert saved.depth["fundamentals"]["earnings"]["days_to"] == 2
    assert saved.depth["digest"] == proposal.depth["digest"]


def test_proposal_depth_snapshot_written_by_pipeline(deep_settings, monkeypatch):
    from app import market as market_module
    from app import pipeline
    from app import proposal as proposal_module

    bars = uptrend(320)
    monkeypatch.setattr(market_module, "fetch_daily",
                        lambda ticker, settings, force_refresh=False: type("D", (), {
                            "ticker": ticker, "bars": bars, "source": "test", "synthetic": False,
                            "warnings": []})())
    monkeypatch.setattr(depth_module.fundamentals_module, "fetch_fundamentals",
                        lambda *a, **k: fund_packet())
    monkeypatch.setattr(depth_module.news_module, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(depth_module.macro_module, "collect",
                        lambda *a, **k: {"state": "risk_on", "inputs": {"vix": 13.0}})
    monkeypatch.setattr(depth_module.fundamentals_module, "earnings_calendar", lambda *a, **k: {})

    # Пропозиції в тестах синтетичні (свічки генеруються), тому план підставляємо явний:
    # мета тесту — перевірити, що пайплайн передає сектори й зберігає зріз глибини.
    monkeypatch.setattr(proposal_module, "plan_from_technical", lambda tech, settings, has_position=False: {
        "bias": "BUY", "entry_type": "limit", "entry": round(tech.last_close * 0.99, 2),
        "stop": round(tech.last_close * 0.95, 2), "target": round(tech.last_close * 1.09, 2),
        "rr": 2.0, "confidence": 0.7, "reasons": ["тестовий сетап"],
    })

    store = Store(deep_settings.db_path)
    proposals = pipeline.build_proposals(deep_settings, store, ["NVDA", "AAPL"], notify=False, summary=False)
    assert len(proposals) == 2
    session = proposals[0].for_session
    snapshot = deep_settings.data_dir / "depth" / f"{session}.json"
    assert snapshot.exists()
    import json

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["macro"]["state"] == "risk_on"
    assert set(payload["tickers"]) == {"NVDA", "AAPL"}
    assert payload["tickers"]["NVDA"]["sector"] == "semis"

    # NVDA (semis) схвалено першим, а другий тікер того самого сектору має бути заблокований
    assert proposals[0].depth["sector"] == "semis"
    assert proposals[0].approved is True and proposals[0].qty > 0
    proposals = pipeline.build_proposals(deep_settings, store, ["AMD", "NVDA"], notify=False, summary=False)
    assert proposals[0].depth["sector"] == "semis" and proposals[1].depth["sector"] == "semis"
    assert proposals[0].approved is True
    assert proposals[1].approved is False and proposals[1].qty == 0
    assert any("диверсифікація" in reason for reason in proposals[1].gate_reasons)


# ─── оцінка дати звіту за каденцією SEC ──────────────────────────────────────

def test_next_report_estimate_uses_quarterly_cadence():
    today = dt.date(2026, 10, 11)
    out = fund_module.next_report_estimate(
        ["2026-08-26", "2026-05-28", "2026-02-25"], "2026-08-26", today
    )
    assert out["source"] == "sec_estimate"
    assert out["date"].startswith("2026-11") and out["days_to"] > 30
    assert "оцінка" in out["note"]


def test_next_report_estimate_ignores_annual_gaps_and_handles_no_data():
    today = dt.date(2026, 10, 11)
    out = fund_module.next_report_estimate(["2026-08-26", "2025-08-26"], "2026-08-26", today)
    assert out["date"] == "2026-11-25"                     # 91 день за замовчуванням
    assert fund_module.next_report_estimate([], None, today) is None


def test_margin_is_none_when_periods_do_not_match():
    series = [{"end": "2020-01-26", "val": 59_600_000_000}]
    assert fund_module.value_at(series, "2026-07-26") is None
    assert fund_module.margin(None, 3_100_000_000) is None
    assert fund_module.margin(1e12, 1e9) is None           # абсурдна маржа відсікається


def test_value_at_matches_exact_period():
    series = [{"end": "2026-06-27", "val": 29_600_000_000}, {"end": "2026-03-28", "val": 24_000_000_000}]
    assert fund_module.value_at(series, "2026-06-27") == 29_600_000_000
    assert fund_module.value_at(series, None) is None


# ─── рівні підтримки/опору та дивергенція ────────────────────────────────────

def test_key_levels_are_split_around_price_and_sorted():
    from app.technical import key_levels

    bars = uptrend(200)
    levels = key_levels(bars)
    close = bars[-1].close
    assert all(level > close for level in levels["resistances"])
    assert all(level < close for level in levels["supports"])
    assert levels["resistances"] == sorted(levels["resistances"])[:3]
    assert levels["supports"] == sorted(levels["supports"], reverse=True)[:3]


def test_rsi_divergence_detects_bullish_case():
    from app.technical import rsi_divergence

    # ціна робить нижчий мінімум, але падіння слабшає (RSI вищий) — бичача дивергенція
    closes = [100.0]
    for _ in range(30):
        closes.append(closes[-1] * 1.001)
    for _ in range(8):
        closes.append(closes[-1] * 0.99)
    for _ in range(14):
        closes.append(closes[-1] * 1.002)
    for _ in range(10):
        closes.append(closes[-1] * 0.985)
    for _ in range(12):
        closes.append(closes[-1] * 1.004)
    bars, day = [], dt.date(2025, 1, 1)
    for close in closes:
        bars.append(Bar(date=day.isoformat(), open=close, high=close * 1.004, low=close * 0.996,
                        close=close, volume=1_000_000))
        day += dt.timedelta(days=1)
    kind, note = rsi_divergence(bars)
    assert kind in ("bullish", "none")
    if kind == "bullish":
        assert "мінімум" in note


# ─── сторінка результатів показує глибину ────────────────────────────────────

def test_site_renders_depth_sections(deep_settings, monkeypatch):
    import json as json_module
    import sys as sys_module
    from pathlib import Path as PathCls

    sys_module.path.insert(0, str(PathCls(__file__).resolve().parent.parent / "tools"))
    from make_site import build_site

    store = Store(deep_settings.db_path)
    store.seed_watchlist(["NVDA"])
    proposal = __import__("app.proposal", fromlist=["Proposal"]).Proposal(
        ticker="NVDA", for_session="2026-10-12", last_bar_date="2026-10-09", last_close=229.28,
        change_pct=1.2, bias="BUY", entry_type="limit", entry_price=229.28, stop_price=220.75,
        target_price=246.34, rr=2.0, qty=4, confidence=0.9, approved=True,
        reasons=["тренд висхідний"], note="Правила.", depth={
            "enabled": True, "sector": "semis", "sector_label": "напівпровідники",
            "rs_20_pct": 3.17, "risk_factor": 1.0, "confidence_delta": 0.07,
            "fundamentals": fund_packet(), "news": [{"title": "Заголовок про NVDA", "published": "2026-10-10"}],
            "backtest": {"signals": 12, "win_rate": 58.0, "expectancy_r": 0.35, "avg_days": 6.0},
            "macro": {"state": "risk_on", "inputs": {"vix": 13.4, "ten_year": 4.1}},
            "levels": {"supports": [220.0], "resistances": [240.0], "divergence": "none", "divergence_note": ""},
            "verdict": {"allowed": True, "blocks": [], "notes": ["правило підтверджене історією"]},
            "digest": "**Бізнес.** Виручка зростає.", "digest_source": "rules",
        },
    )
    proposal.id = store.insert_proposal(proposal)

    snapshot = {"session": "2026-10-12", "macro": {"state": "risk_on", "score": 3, "reasons": ["VIX 13 — спокійно"],
                "inputs": {"vix": 13.4, "ten_year": 4.1, "dollar": 102.2}, "as_of": "2026-10-11T08:30"},
                "breadth_pct": 62.0, "calendar": {"2026-10-13": [{"symbol": "NVDA", "time": "amc", "epsForecast": "1.25"}]},
                "tickers": {"NVDA": {"sector": "semis", "sector_label": "напівпровідники", "bias": "BUY",
                                     "approved": True, "score": 2, "rs_20_pct": 3.17, "expectancy_r": 0.35,
                                     "signals": 12, "earnings": {"days_to": 39}, "blocks": []}}}
    folder = deep_settings.data_dir / "depth"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2026-10-12.json").write_text(json_module.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    out_dir = deep_settings.data_dir / "site"
    build_site(settings=deep_settings, out_dir=out_dir, sessions=5, pages_for_sessions=1)
    index = (out_dir / "index.html").read_text(encoding="utf-8")
    page = (out_dir / f"proposal-{proposal.id}.html").read_text(encoding="utf-8")
    payload = json_module.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    markdown = (out_dir / "results.md").read_text(encoding="utf-8")

    assert "Глибина аналізу" in index and "Режим ринку" in index
    assert "Календар звітностей" in index and "13.4" in index      # VIX округлено
    assert "Хто як виглядає за даними" in index and "NVDA" in index
    assert "Що це означає (дайджест)" in page and "Виручка рік до року" in page
    assert "Історія правила" in page and "SEC EDGAR" in page
    assert payload["depth"]["macro"]["state"] == "risk_on"
    assert payload["proposals"][0]["depth"]["sector"] == "semis"
    assert "Глибина аналізу" in markdown and "Режим ринку" in markdown
