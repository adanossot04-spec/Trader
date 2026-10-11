"""Розклад: запуск за годину до відкриття US-ринку (у таймзоні біржі, а не Києва)."""

from __future__ import annotations

import datetime as dt

import pytest
from apscheduler.triggers.cron import CronTrigger

from app import scheduler as scheduler_module
from app.db import Store
from app.scheduler import CronSpecError, trigger_from_crontab

ET = "America/New_York"


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)


@pytest.fixture
def live_settings(settings):
    """Той самий Settings, але з увімкненим розкладом (conftest його глушить)."""
    settings.config["schedule"]["enabled"] = True
    return settings


# ─── конфігурація вікон ───────────────────────────────────────────────────────

def test_default_anchor_is_pre_open(live_settings):
    jobs = live_settings.schedule_jobs()
    assert [job["id"] for job in jobs] == ["pre_open"]
    pre = jobs[0]
    assert pre["cron"] == "30 8 * * 1-5"
    assert pre["timezone"] == ET
    assert "60" in pre["title"] or "годину" in pre["title"]


def test_schedule_summary_mentions_window(live_settings):
    summary = live_settings.schedule_summary()
    assert "8:30" in summary or "30 8" in summary
    assert "America/New_York" in summary


def test_anchor_both_adds_after_close_window(live_settings):
    live_settings.config["schedule"]["anchor"] = "both"
    jobs = {job["id"]: job for job in live_settings.schedule_jobs()}
    assert set(jobs) == {"pre_open", "after_close"}
    assert jobs["after_close"]["timezone"] == "Europe/Kyiv"
    assert jobs["after_close"]["cron"] == "15 23 * * 1-5"


def test_anchor_after_close_only(live_settings):
    live_settings.config["schedule"]["anchor"] = "after_close"
    assert [job["id"] for job in live_settings.schedule_jobs()] == ["after_close"]


def test_disabled_schedule_has_no_windows(settings):
    settings.config["schedule"]["enabled"] = False
    assert settings.schedule_jobs() == []
    assert "вимкнено" in settings.schedule_summary()


# ─── cron-тригер дійсно стріляє за годину до відкриття ───────────────────────

def test_pre_open_trigger_fires_at_0830_et():
    trigger = trigger_from_crontab("30 8 * * 1-5", ET)
    # нд 11.10 16:00 ET → найближчий запуск має бути в понеділок
    fire = trigger.get_next_fire_time(None, at(2026, 10, 11, 20, 0))
    assert fire is not None
    assert fire.astimezone(dt.timezone.utc) == at(2026, 10, 12, 12, 30)   # 8:30 ET = 12:30 UTC
    assert fire.strftime("%H:%M") == "08:30"


def test_weekdays_are_monday_to_friday():
    """Головний баг, який тут ловиться: APScheduler рахує 0=понеділок, а cron — 0=неділя."""
    trigger = trigger_from_crontab("30 8 * * 1-5", ET)
    fires = []
    cursor = None
    for _ in range(6):
        cursor = trigger.get_next_fire_time(cursor, cursor or at(2026, 10, 11, 20, 0))
        fires.append(cursor.astimezone(dt.timezone.utc).date())
    assert all(day.weekday() < 5 for day in fires), f"субота потрапила у розклад: {fires}"
    assert fires[0].weekday() == 0, "перший запуск має бути в понеділок"
    assert fires[4].weekday() == 4, "п'ятий — у п'ятницю"


def test_from_crontab_would_be_wrong():
    """Документуємо пастку: вбудований from_crontab дає вт–сб, тому ми ним не користуємось."""
    naive = CronTrigger.from_crontab("30 8 * * 1-5", timezone=ET)
    fired = naive.get_next_fire_time(None, at(2026, 10, 11, 20, 0))
    assert fired.astimezone(dt.timezone.utc).date().weekday() == 1     # вівторок, а не понеділок
    assert trigger_from_crontab("30 8 * * 1-5", ET).get_next_fire_time(
        None, at(2026, 10, 11, 20, 0)
    ).astimezone(dt.timezone.utc).date().weekday() == 0                # а наш — понеділок


def test_day_of_week_names_and_numbers_equivalent():
    by_number = trigger_from_crontab("30 8 * * 1-5", ET)
    by_name = trigger_from_crontab("30 8 * * mon-fri", ET)
    moment = at(2026, 10, 11, 20, 0)
    assert by_number.get_next_fire_time(None, moment) == by_name.get_next_fire_time(None, moment)


def test_day_of_week_lists_and_sunday_zero():
    assert scheduler_module._translate_day_of_week("0") == "sun"
    assert scheduler_module._translate_day_of_week("7") == "sun"
    assert scheduler_module._translate_day_of_week("1,3,5") == "mon,wed,fri"
    assert scheduler_module._translate_day_of_week("0-4") == "sun-thu"   # діапазон лишається діапазоном
    assert scheduler_module._translate_day_of_week("*") == "*"


def test_cron_spec_errors_are_clear():
    with pytest.raises(CronSpecError):
        trigger_from_crontab("30 8 * *", ET)
    with pytest.raises(CronSpecError):
        trigger_from_crontab("30 8 * * 9", ET)
    with pytest.raises(CronSpecError):
        trigger_from_crontab("30 8 * * понеділок", ET)


def test_minutes_and_hours_from_config_are_used(live_settings):
    live_settings.config["schedule"]["pre_open_cron"] = "45 9 * * 1-5"
    trigger = trigger_from_crontab(
        live_settings.schedule_jobs()[0]["cron"], live_settings.schedule_jobs()[0]["timezone"]
    )
    fire = trigger.get_next_fire_time(None, at(2026, 10, 11, 20, 0))
    assert fire.strftime("%H:%M") == "09:45" and fire.weekday() == 0


def test_pre_open_trigger_survives_dst_mismatch_weeks():
    """США й Україна переходять на літній час у різні дати.

    У «перехідні» тижні київський cron зміщується на годину, а нью-йоркський —
    ні: 8:30 ET завжди за 60 хв до відкриття.
    """
    trigger = trigger_from_crontab("30 8 * * 1-5", ET)
    # 15 березня 2026 — США вже на EDT (UTC-4), Україна ще на EET (UTC+2)
    spring = trigger.get_next_fire_time(None, at(2026, 3, 15, 23, 0))
    assert spring.astimezone(dt.timezone.utc) == at(2026, 3, 16, 12, 30)
    assert spring.strftime("%H:%M") == "08:30"
    # 28 жовтня 2026 — США ще на EDT, Україна вже на EET
    autumn = trigger.get_next_fire_time(None, at(2026, 10, 27, 23, 0))
    assert autumn.astimezone(dt.timezone.utc) == at(2026, 10, 28, 12, 30)
    assert autumn.strftime("%H:%M") == "08:30"


def test_kyiv_cron_would_drift_in_march():
    """Причина вибору America/New_York: «15:30 за Києвом» у березні дає 9:30 ET — рівно відкриття."""
    from zoneinfo import ZoneInfo

    kyiv_trigger = trigger_from_crontab("30 15 * * 1-5", "Europe/Kyiv")
    fired = kyiv_trigger.get_next_fire_time(None, at(2026, 3, 15, 23, 0))
    et_time = fired.astimezone(ZoneInfo(ET))
    assert et_time.strftime("%H:%M") == "09:30", "київський cron у перехідний тиждень з'їжджає на годину"
    assert fired.astimezone(dt.timezone.utc) == at(2026, 3, 16, 13, 30)

    et_trigger = trigger_from_crontab("30 8 * * 1-5", ET)
    correct = et_trigger.get_next_fire_time(None, at(2026, 3, 15, 23, 0)).astimezone(ZoneInfo(ET))
    assert correct.strftime("%H:%M") == "08:30", "а біржовий cron лишається за 60 хв до відкриття"


# ─── сам планувальник ────────────────────────────────────────────────────────

def start_paused(sched):
    """Реєструємо задачі в справжньому планувальнику, але не даємо їм виконуватись."""
    sched.start(paused=True)
    return sched


def test_build_scheduler_registers_pre_open_job(live_settings):
    store = Store(live_settings.db_path)
    sched = start_paused(scheduler_module.build_scheduler(live_settings, store))
    jobs = {job.id: job for job in sched.get_jobs()}
    assert "pre_open" in jobs
    job = jobs["pre_open"]
    assert str(job.trigger.timezone) == ET
    sched.shutdown(wait=False)


def test_build_scheduler_disabled_registers_nothing(settings):
    settings.config["schedule"]["enabled"] = False
    sched = start_paused(scheduler_module.build_scheduler(settings, Store(settings.db_path)))
    assert sched.get_jobs() == []
    sched.shutdown(wait=False)


def test_scheduled_job_builds_proposals(live_settings, monkeypatch):
    """Запускаємо тіло задачі напряму — перевіряємо, що вона справді готує пропозиції."""
    from app import pipeline

    store = Store(live_settings.db_path)
    sched = start_paused(scheduler_module.build_scheduler(live_settings, store))

    calls: list[list[str]] = []

    def fake_build(settings_arg, store_arg, tickers=None, **kwargs):
        ticks = list(tickers if tickers is not None else store_arg.watchlist())
        calls.append(ticks)
        return [type("P", (), {"for_session": "2026-10-12", "actionable": True})() for _ in ticks]

    monkeypatch.setattr(pipeline, "build_proposals", fake_build)
    monkeypatch.setattr(pipeline, "settle_pending", lambda *a, **k: [])

    store.seed_watchlist(["NVDA", "AAPL"])
    sched.get_job("pre_open").func()          # викликаємо задачу як звичайну функцію

    assert calls == [["AAPL", "NVDA"]]        # watchlist відсортований
    sched.shutdown(wait=False)


def test_job_failure_does_not_kill_scheduler(live_settings, monkeypatch):
    from app import pipeline

    store = Store(live_settings.db_path)
    sched = start_paused(scheduler_module.build_scheduler(live_settings, store))
    monkeypatch.setattr(pipeline, "settle_pending", lambda *a, **k: [])
    monkeypatch.setattr(
        pipeline, "build_proposals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("джерело впало"))
    )
    sched.get_job("pre_open").func()          # не має кидати далі
    sched.shutdown(wait=False)


def test_job_logs_minutes_to_open(live_settings, caplog, monkeypatch):
    from app import pipeline

    store = Store(live_settings.db_path)
    sched = start_paused(scheduler_module.build_scheduler(live_settings, store))
    monkeypatch.setattr(pipeline, "settle_pending", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "build_proposals", lambda *a, **k: [])
    with caplog.at_level("INFO"):
        sched.get_job("pre_open").func()
    assert "відкриття US-ринку" in caplog.text
    sched.shutdown(wait=False)
