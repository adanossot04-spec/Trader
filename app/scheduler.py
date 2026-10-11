"""Розклад запусків: головне вікно — за 60 хвилин до відкриття US-ринку.

Дві важливі деталі, які легко зробити неправильно:

1. **Таймзона біржі, а не Києва.** США й Україна переходять на літній час у різні
   дати (березень / кінець жовтня). Якщо задати «15:30 за Києвом», то в ці кілька
   тижнів прогін зміститься на годину й потрапить рівно на відкриття. У таймзоні
   America/New_York 8:30 — це завжди рівно 60 хвилин до відкриття.

2. **Нумерація днів тижня.** APScheduler рахує 0=понеділок, а стандартний cron —
   0=неділя. Тому `CronTrigger.from_crontab("30 8 * * 1-5")` дає НЕ пн–пт, а вт–сб:
   бот пропускав би понеділок і працював у суботу. Нижче власний переклад
   cron-виразу в APScheduler-тригер за конвенцією стандартного cron.
"""

from __future__ import annotations

import logging
import re

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import pipeline
from .db import Store

log = logging.getLogger("assistant.scheduler")

# конвенція стандартного cron: 0 або 7 = неділя
_CRON_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


class CronSpecError(ValueError):
    """Незрозумілий cron-вираз."""


def _day_token(token: str) -> str:
    """'1' → 'mon' (стандартний cron), 'MON' → 'mon', 'mon' → 'mon'."""
    token = token.strip().lower()
    if token in _CRON_DAYS:
        return token
    if token.isdigit():
        number = int(token)
        if number == 7:
            number = 0
        if 0 <= number <= 6:
            return _CRON_DAYS[number]
        raise CronSpecError(f"день тижня поза діапазоном 0-7: {token}")
    raise CronSpecError(f"нерозпізнаний день тижня: {token}")


def _translate_day_of_week(field: str) -> str:
    """Перекладає поле дня тижня зі стандартного cron у назви для APScheduler."""
    field = field.strip().lower()
    if field in ("*", "?"):
        return "*"
    parts: list[str] = []
    for chunk in field.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "/" in chunk:                      # напр. 1-5/2 → крок усередині діапазону
            chunk = chunk.split("/", 1)[1]     # для тижнів крок майже не використовують
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            parts.append(f"{_day_token(start)}-{_day_token(end)}")
        else:
            parts.append(_day_token(chunk))
    if not parts:
        raise CronSpecError(f"порожнє поле дня тижня: {field!r}")
    return ",".join(parts)


def trigger_from_crontab(expression: str, timezone: str) -> CronTrigger:
    """Cron-вираз у стандартному форматі (5 полів) → APScheduler-тригер.

    Відмінність від CronTrigger.from_crontab: дні тижня нумеруються як у звичайному
    cron (0=неділя), тому «1-5» — це справді понеділок-п'ятниця.
    """
    fields = re.split(r"\s+", expression.strip())
    if len(fields) != 5:
        raise CronSpecError(f"очікується 5 полів у cron-виразі, отримано {len(fields)}: {expression!r}")
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_translate_day_of_week(day_of_week),
        timezone=timezone,
    )


def build_scheduler(settings, store: Store) -> BackgroundScheduler:
    """Планувальник із вікон, заданих у config.yaml → schedule."""
    cfg = settings.schedule
    jobs = settings.schedule_jobs()
    scheduler = BackgroundScheduler(timezone=cfg.get("market_timezone", "America/New_York"))

    if not jobs:
        log.info("Планувальник вимкнено в config.yaml (schedule.enabled=false)")
        return scheduler

    def make_job(kind: str):
        def job() -> None:
            """Готує пропозиції на найближчу сесію."""
            from . import market as market_module

            minutes = market_module.minutes_to_open()
            log.info(
                "Запуск (%s): готую пропозиції; до відкриття US-ринку %d хв (≈%.1f год)",
                kind, minutes, minutes / 60,
            )
            try:
                if cfg.get("settle_before_run", True):
                    pipeline.settle_pending(settings, store)
                proposals = pipeline.build_proposals(
                    settings, store, summary=bool(cfg.get("notify_summary", True))
                )
                prepared = sum(1 for p in proposals if p.actionable)
                session = proposals[0].for_session if proposals else "—"
                log.info("Готово: сесія %s, пропозицій %d, з планом %d", session, len(proposals), prepared)
            except Exception:  # noqa: BLE001
                log.exception("Підготовка пропозицій (%s) упала", kind)

        return job

    for spec in jobs:
        trigger = trigger_from_crontab(spec["cron"], spec["timezone"])
        scheduler.add_job(make_job(spec["id"]), trigger, id=spec["id"], replace_existing=True)
        log.info("Заплановано «%s»: cron «%s» (%s)", spec["title"], spec["cron"], spec["timezone"])

    if any(spec["id"] == "pre_open" for spec in jobs):
        from . import market as market_module

        log.info("До найближчого відкриття ринку: %d хв", market_module.minutes_to_open())
    return scheduler
