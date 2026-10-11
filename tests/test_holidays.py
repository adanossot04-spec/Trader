"""Календар NYSE: свята, переноси, «наступне відкриття» без неіснуючих сесій.

Без цього бот у День подяки вважав би, що до відкриття лишилась година,
і надсилав би план на сесію, якої не буде.
"""

from __future__ import annotations

import datetime as dt
from datetime import date

import pytest

from app import market
from app.market import (
    Bar,
    ET,
    is_trading_day,
    market_holiday_name,
    next_open_datetime,
    next_session_date,
    us_market_holidays,
)


@pytest.mark.parametrize(
    "day, expected",
    [
        (date(2026, 1, 1), "Новий рік"),
        (date(2026, 1, 19), "Мартін Лютер Кінг"),        # 3-й понеділок січня
        (date(2026, 2, 16), "День президентів"),         # 3-й понеділок лютого
        (date(2026, 4, 3), "Страсна п'ятниця"),          # Великдень 2026 — 5 квітня
        (date(2026, 5, 25), "Memorial Day"),             # останній понеділок травня
        (date(2026, 6, 19), "Juneteenth"),
        (date(2026, 7, 3), "День незалежності"),         # 4 липня — субота, перенос на п'ятницю
        (date(2026, 9, 7), "День праці"),
        (date(2026, 11, 26), "День подяки"),             # 4-й четвер листопада
        (date(2026, 12, 25), "Різдво"),
    ],
)
def test_holidays_2026(day, expected):
    assert is_trading_day(day) is False
    assert market_holiday_name(day) == expected


def test_easter_and_good_friday_shift_between_years():
    assert market.easter_sunday(2026) == date(2026, 4, 5)
    assert market.easter_sunday(2027) == date(2027, 3, 28)
    assert date(2027, 3, 26) in us_market_holidays(2027)      # Страсна п'ятниця 2027
    assert market_holiday_name(date(2027, 3, 26)) == "Страсна п'ятниця"


def test_observed_rules_move_holidays_off_the_weekend():
    # 2027: 25 грудня — субота → біржа зачинена в п'ятницю 24-го
    assert market_holiday_name(date(2027, 12, 24)) == "Різдво"
    # 2027: 19 червня — субота → перенос на п'ятницю 18-го
    assert market_holiday_name(date(2027, 6, 18)) == "Juneteenth"
    # 2027: 4 липня — неділя → перенос на понеділок 5-го
    assert market_holiday_name(date(2027, 7, 5)) == "День незалежності"


def test_normal_weekdays_are_trading_days():
    for day in (date(2026, 10, 12), date(2026, 10, 13), date(2026, 11, 27), date(2027, 3, 29)):
        assert is_trading_day(day) is True
        assert market_holiday_name(day) is None


@pytest.mark.parametrize("day", [date(2026, 10, 17), date(2026, 10, 18)])
def test_weekend_is_not_trading_day(day):
    assert is_trading_day(day) is False
    assert market_holiday_name(day) is None       # це не свято, просто вихідний


def test_next_open_skips_thanksgiving():
    moment = dt.datetime(2026, 11, 26, 8, 30, tzinfo=ET)      # ранок Дня подяки
    assert next_open_datetime(moment) == dt.datetime(2026, 11, 27, 9, 30, tzinfo=ET)
    assert market.minutes_to_open(moment) > 24 * 60           # не «0 хв», а наступний день


def test_next_open_on_holiday_evening_skips_friday_to_monday():
    moment = dt.datetime(2026, 12, 25, 18, 0, tzinfo=ET)      # Різдво, п'ятниця, вечір
    assert next_open_datetime(moment) == dt.datetime(2026, 12, 28, 9, 30, tzinfo=ET)


def test_session_is_closed_on_holiday():
    assert market.session_is_open(dt.datetime(2026, 7, 3, 12, 0, tzinfo=ET)) is False
    assert market.session_is_open(dt.datetime(2026, 7, 6, 12, 0, tzinfo=ET)) is True


def _bars(dates: list[str]) -> list[Bar]:
    return [Bar(date=day, open=100.0, high=101.0, low=99.0, close=100.5, volume=1_000) for day in dates]


def test_next_session_after_wednesday_before_thanksgiving():
    bars = _bars(["2026-11-23", "2026-11-24", "2026-11-25"])
    assert next_session_date(bars) == date(2026, 11, 27)      # 26-го біржа зачинена


def test_next_session_skips_independence_weekend():
    bars = _bars(["2026-07-01", "2026-07-02"])
    assert next_session_date(bars) == date(2026, 7, 6)        # 3-є свято + вихідні
