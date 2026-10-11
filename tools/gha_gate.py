"""Ворота для GitHub Actions: чи запускати прогін саме зараз.

GitHub Actions виконує розклад у UTC і з затримкою (часто 3–20 хвилин). Тому
workflow має кілька cron-вікон на добу, а рішення «працювати чи ні» ухвалює цей
скрипт — за часом біржі (America/New_York), з урахуванням свят NYSE.

Правила:
  • поза вікном [07:50, 09:05] ET — не працюємо (влітку спрацьовує одне вікно,
    взимку інше; решта просто відсіюються);
  • вихідний або свято NYSE — не працюємо;
  • якщо план на цю сесію вже побудовано менш ніж 45 хв тому (і не --force) —
    не дублюємо повідомлення в Telegram;
  • --force ігнорує вікно й свіжість (ручний запуск із workflow_dispatch).

Вивід — рядки KEY=VALUE, готові для $GITHUB_OUTPUT:

    run=yes|no
    reason=<пояснення українською>
    session=2026-10-12
    minutes_to_open=78
    et=2026-10-12T08:11:00-04:00
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import market as market_module          # noqa: E402
from app.db import Store                         # noqa: E402
from app.settings import load_settings           # noqa: E402

WINDOW_START = time(7, 50)
WINDOW_END = time(9, 5)
FRESH_MINUTES = 45


def decide(
    now: datetime,
    *,
    force: bool = False,
    fresh_plan_at: str | None = None,
    window: tuple[time, time] = (WINDOW_START, WINDOW_END),
) -> dict[str, str]:
    day = now.date()
    clock = now.time()
    session = day.isoformat() if market_module.is_trading_day(day) else (
        market_module.next_trading_day(day).isoformat()
    )
    info = {
        "et": now.isoformat(timespec="seconds"),
        "session": session,
        "minutes_to_open": str(market_module.minutes_to_open(now)),
        "window": f"{window[0]:%H:%M}-{window[1]:%H:%M} ET",
    }

    if force:
        info.update(run="yes", reason="ручний запуск (--force): вікно й свіжість ігноруються")
        return info

    if not market_module.is_trading_day(day):
        holiday = market_module.market_holiday_name(day)
        if holiday:
            reason = f"біржа зачинена: {holiday}"
        elif day.weekday() >= 5:
            reason = "вихідний (субота/неділя)"
        else:
            reason = "біржа зачинена"
        info.update(run="no", reason=reason)
        return info

    if clock < window[0]:
        info.update(run="no", reason=(
            f"занадто рано: {clock:%H:%M} ET, вікно починається о {window[0]:%H:%M} ET "
            f"(до відкриття {market_module.minutes_to_open(now)} хв) — дочекаємось наступного вікна"
        ))
        return info

    if clock > time(9, 30):
        info.update(run="no", reason=f"ринок уже відкрито ({clock:%H:%M} ET) — план на сьогодні не має сенсу")
        return info

    if clock > window[1]:
        info.update(run="no", reason=(
            f"пізно для премаркет-перевірки: {clock:%H:%M} ET (вікно до {window[1]:%H:%M} ET). "
            "Запустіть вручну через workflow_dispatch, якщо план усе ще потрібен"
        ))
        return info

    if fresh_plan_at:
        try:
            built = datetime.fromisoformat(fresh_plan_at)
        except ValueError:
            built = None
        if built is not None:
            if built.tzinfo is None:
                built = built.replace(tzinfo=now.tzinfo)
            age = (now - built.astimezone(now.tzinfo)).total_seconds() / 60
            if 0 <= age < FRESH_MINUTES:
                info.update(run="no", reason=(
                    f"план на цю сесію вже побудовано {age:.0f} хв тому — "
                    f"не дублюємо (є --force для примусового перерахунку)"
                ))
                return info

    info.update(run="yes", reason=(
        f"у вікні {window[0]:%H:%M}-{window[1]:%H:%M} ET: {clock:%H:%M} ET, "
        f"до відкриття {market_module.minutes_to_open(now)} хв — будуємо план на {market_module.session_label_uk(day)}"
    ))
    return info


def _fresh_plan_at(settings) -> str | None:
    """Коли востаннє будували план на найближчу (сьогоднішню) сесію."""
    try:
        store = Store(settings.db_path)
        session = market_module.now_et().date().isoformat()
        items = store.proposals_for_session(session)
        if not items:
            return None
        return max((p.created_at for p in items if p.created_at), default=None)
    except Exception:                                  # noqa: BLE001 — БД може не існувати
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ворота запуску для GitHub Actions")
    parser.add_argument("--force", action="store_true", help="ігнорувати вікно й свіжість плану")
    parser.add_argument("--output", default=os.environ.get("GITHUB_OUTPUT", ""),
                        help="файл для KEY=VALUE (типово $GITHUB_OUTPUT)")
    args = parser.parse_args(argv)

    settings = load_settings()
    info = decide(
        market_module.now_et(),
        force=args.force,
        fresh_plan_at=_fresh_plan_at(settings),
    )

    lines = [f"{key}={value}" for key, value in info.items()]
    text = "\n".join(lines)
    print(text)
    if args.output:
        with open(args.output, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(f"→ рішення: {'ПРАЦЮЄМО' if info['run'] == 'yes' else 'ПРОПУСКАЄМО'} — {info['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
