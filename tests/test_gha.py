"""Ворота запуску для GitHub Actions: чотири cron-вікна, а працює лише одне.

GitHub виконує cron у UTC і з затримкою, тому рішення «працювати зараз чи ні»
ухвалює tools/gha_gate.py за часом біржі. Тести фіксують усі гілки.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from app.market import ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from gha_gate import FRESH_MINUTES, WINDOW_END, WINDOW_START, decide  # noqa: E402


def _et(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=ET)


# ─── літній час (EDST): цільове вікно — 12:10 і 12:40 UTC ────────────────────

def test_edst_prime_window_runs():
    info = decide(_et(2026, 10, 12, 8, 12))               # 12:12 UTC
    assert info["run"] == "yes"
    assert info["session"] == "2026-10-12"
    assert "78 хв" in info["reason"]
    assert int(info["minutes_to_open"]) == 78


def test_edst_later_window_is_too_late():
    info = decide(_et(2026, 10, 12, 9, 12))               # 13:12 UTC
    assert info["run"] == "no"
    assert "пізно" in info["reason"]


# ─── зимовий час (EST): цільове вікно — 13:10 і 13:40 UTC ───────────────────

def test_est_early_window_waits():
    info = decide(_et(2026, 12, 7, 7, 12))                # 12:12 UTC = 07:12 ET узимку
    assert info["run"] == "no"
    assert "занадто рано" in info["reason"]


def test_est_prime_window_runs():
    info = decide(_et(2026, 12, 7, 8, 12))                # 13:12 UTC = 08:12 ET
    assert info["run"] == "yes"
    assert info["session"] == "2026-12-07"


def test_both_seasons_land_in_the_same_window():
    """У кожному сезоні цільові вікна дають 08:10 і 08:40 ET — не раніше й не пізніше.

    Друге вікно (08:40) — страхувальне: якщо перший прогін упав або GitHub
    затримався, план усе одно буде. Від дублів у Telegram захищає перевірка
    свіжості плану (див. test_fresh_plan_is_not_duplicated).
    """
    edst = [_et(2026, 10, 12, h, m) for h, m in ((8, 10), (8, 40), (9, 10), (9, 40))]
    est = [_et(2026, 12, 7, h, m) for h, m in ((7, 10), (7, 40), (8, 10), (8, 40))]
    for moments in (edst, est):
        inside = sorted(m for m in moments if WINDOW_START <= m.time() <= WINDOW_END)
        assert [m.strftime("%H:%M") for m in inside] == ["08:10", "08:40"]
        assert inside[0].time() < WINDOW_END


# ─── свята й вихідні ────────────────────────────────────────────────────────

def test_holiday_is_skipped():
    info = decide(_et(2026, 11, 26, 8, 12))               # День подяки
    assert info["run"] == "no"
    assert "День подяки" in info["reason"]
    assert info["session"] == "2026-11-27"                # наступна реальна сесія


def test_weekend_is_skipped():
    info = decide(_et(2026, 10, 17, 8, 12))               # субота
    assert info["run"] == "no"
    assert "вихідний" in info["reason"]


# ─── захист від дублювання повідомлень ──────────────────────────────────────

def test_fresh_plan_is_not_duplicated():
    built = _et(2026, 10, 12, 8, 12).isoformat()
    info = decide(_et(2026, 10, 12, 8, 40), fresh_plan_at=built)
    assert info["run"] == "no"
    assert "вже побудовано" in info["reason"]


def test_stale_plan_is_rebuilt():
    built = _et(2026, 10, 12, 7, 0).isoformat()           # 100 хв тому
    info = decide(_et(2026, 10, 12, 8, 40), fresh_plan_at=built)
    assert info["run"] == "yes"
    assert FRESH_MINUTES < 100


def test_force_ignores_window_holiday_and_freshness():
    info = decide(_et(2026, 11, 26, 14, 30), fresh_plan_at=_et(2026, 11, 26, 14, 0).isoformat(),
                  force=True)
    assert info["run"] == "yes"
    assert "--force" in info["reason"]


# ─── вивід для $GITHUB_OUTPUT ───────────────────────────────────────────────

def test_outputs_are_written_for_actions(tmp_path, monkeypatch):
    """Скрипт має віддати run=yes|no у $GITHUB_OUTPUT — саме це читає workflow."""
    from gha_gate import main

    out = tmp_path / "gh_output"
    monkeypatch.setattr(
        "gha_gate.load_settings",
        lambda: type("S", (), {"db_path": tmp_path / "немає.db"})(),
    )
    monkeypatch.setattr("gha_gate.market_module.now_et", lambda: _et(2026, 10, 12, 8, 12))
    assert main(["--output", str(out)]) == 0

    text = out.read_text(encoding="utf-8")
    assert "run=yes" in text
    assert "session=2026-10-12" in text
    assert "minutes_to_open=78" in text
    assert "reason=" in text
