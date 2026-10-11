"""Добові свічки з безкоштовних джерел + кеш + офлайн-фолбек.

Джерела (у порядку з config.yaml → data.sources):
  • yahoo        — без ключа взагалі (публічний chart API Yahoo Finance)
  • twelvedata   — безкоштовний ключ (800 запитів/добу) → TWELVEDATA_API_KEY
  • alphavantage — безкоштовний ключ (25 запитів/добу) → ALPHA_VANTAGE_API_KEY
  • synthetic    — детермінований офлайн-ряд, щоб конвеєр працював без мережі (позначається!)

Дані кешуються у data/cache/, щоб не смикати безкоштовні ліміти.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from datetime import date as date_cls, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("assistant.market")

ET = ZoneInfo("America/New_York")
_UA = {"User-Agent": "Mozilla/5.0 (compatible; TradingAssistant/0.2; personal research)"}

_BASE_PRICES = {
    "NVDA": 229.28, "AAPL": 248.7, "MSFT": 512.6, "SPY": 640.2, "QQQ": 578.9,
    "TSLA": 331.5, "AMZN": 224.7, "GOOGL": 208.3, "META": 742.1, "AMD": 168.2,
}


@dataclass
class Bar:
    date: str          # YYYY-MM-DD (сесія за календарем біржі)
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketData:
    ticker: str
    bars: list[Bar]
    source: str
    fetched_at: str
    synthetic: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def last(self) -> Bar:
        return self.bars[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "synthetic": self.synthetic,
            "warnings": self.warnings,
            "bars": [b.to_dict() for b in self.bars],
        }


# ─── час сесій ────────────────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(ET)


# ─── календар торгів NYSE ────────────────────────────────────────────────────
# Потрібен, бо cron «пн-пт» не знає про свята: без нього бот у День подяки
# вважав би, що до відкриття лишилась година, і «планував» неіснуючу сесію.

def easter_sunday(year: int) -> date_cls:
    """Великдень (західний обряд) — потрібен для Страсної п'ятниці (біржа зачинена)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date_cls(year, month, day + 1)


def _observed(day: date_cls) -> date_cls:
    """Перенос свята з вихідного: субота → п'ятниця, неділя → понеділок."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date_cls:
    day = date_cls(year, month, 1)
    shift = (weekday - day.weekday()) % 7
    return day + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date_cls:
    day = date_cls(year, month, 28)
    while (day + timedelta(days=1)).month == month:
        day += timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


@lru_cache(maxsize=32)
def us_market_holidays(year: int) -> frozenset[date_cls]:
    """Свята, коли NYSE не працює (правила NYSE, включно з переносами)."""
    return frozenset({
        _observed(date_cls(year, 1, 1)),          # Новий рік
        _nth_weekday(year, 1, 0, 3),              # Мартін Лютер Кінг (3-й пн січня)
        _nth_weekday(year, 2, 0, 3),              # День президентів (3-й пн лютого)
        easter_sunday(year) - timedelta(days=2),  # Страсна п'ятниця
        _last_weekday(year, 5, 0),                # Memorial Day (останній пн травня)
        _observed(date_cls(year, 6, 19)),         # Juneteenth
        _observed(date_cls(year, 7, 4)),          # День незалежності
        _nth_weekday(year, 9, 0, 1),              # День праці (1-й пн вересня)
        _nth_weekday(year, 11, 3, 4),             # День подяки (4-й чт листопада)
        _observed(date_cls(year, 12, 25)),        # Різдво
    })


def market_holiday_name(day: date_cls) -> str | None:
    """Українська назва свята, якщо біржа зачинена саме цього дня."""
    rules = (
        ("Новий рік", _observed(date_cls(day.year, 1, 1))),
        ("Мартін Лютер Кінг", _nth_weekday(day.year, 1, 0, 3)),
        ("День президентів", _nth_weekday(day.year, 2, 0, 3)),
        ("Страсна п'ятниця", easter_sunday(day.year) - timedelta(days=2)),
        ("Memorial Day", _last_weekday(day.year, 5, 0)),
        ("Juneteenth", _observed(date_cls(day.year, 6, 19))),
        ("День незалежності", _observed(date_cls(day.year, 7, 4))),
        ("День праці", _nth_weekday(day.year, 9, 0, 1)),
        ("День подяки", _nth_weekday(day.year, 11, 3, 4)),
        ("Різдво", _observed(date_cls(day.year, 12, 25))),
    )
    for name, when in rules:
        if when == day:
            return name
    return None


def is_trading_day(day: date_cls) -> bool:
    return day.weekday() < 5 and day not in us_market_holidays(day.year)


def next_trading_day(day: date_cls) -> date_cls:
    candidate = day + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def session_is_open(moment: datetime | None = None) -> bool:
    moment = moment or now_et()
    if not is_trading_day(moment.date()):
        return False
    return time(9, 30) <= moment.time() <= time(16, 0)


OPEN_ET = time(9, 30)
CLOSE_ET = time(16, 0)


def completed_sessions_only(bars: list[Bar], moment: datetime | None = None) -> list[Bar]:
    """Прибирає свічку поточної (ще не закритої) сесії.

    Добова свічка стає остаточною лише після закриття о 16:00 ET. Якщо прогін
    відбувається вранці (за годину до відкриття) або вдень, сьогоднішня свічка
    ще неповна — її не можна брати за основу плану.
    """
    moment = moment or now_et()
    if moment.weekday() < 5 and moment.time() < CLOSE_ET:
        today = moment.date().isoformat()
        return [bar for bar in bars if bar.date < today]
    return bars


def next_open_datetime(moment: datetime | None = None) -> datetime:
    """Найближче відкриття US-ринку (9:30 ET) з урахуванням свят NYSE."""
    moment = moment or now_et()
    day, clock = moment.date(), moment.time()
    if is_trading_day(day) and clock < OPEN_ET:
        return datetime.combine(day, OPEN_ET, tzinfo=ET)
    return datetime.combine(next_trading_day(day), OPEN_ET, tzinfo=ET)


def minutes_to_open(moment: datetime | None = None) -> int:
    moment = moment or now_et()
    delta = next_open_datetime(moment) - moment
    return max(0, int(delta.total_seconds() // 60))


def session_label_uk(day: date_cls) -> str:
    names = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
    return f"{day.day:02d}.{day.month:02d}.{day.year} ({names[day.weekday()]})"


def next_weekday(day: date_cls) -> date_cls:
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


# ─── кеш ──────────────────────────────────────────────────────────────────────

def _cache_path(cache_dir: Path, ticker: str) -> Path:
    return cache_dir / f"{ticker.upper()}_1d.json"


def _read_cache(cache_dir: Path, ticker: str, ttl_min: int) -> dict[str, Any] | None:
    path = _cache_path(cache_dir, ticker)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(payload["fetched_at"])).total_seconds() / 60
    except (KeyError, ValueError):
        return None
    if age_min > ttl_min:
        return None
    return payload


def _write_cache(cache_dir: Path, ticker: str, payload: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        _cache_path(cache_dir, ticker).write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.warning("не вдалося записати кеш для %s: %s", ticker, exc)


# ─── провайдери ───────────────────────────────────────────────────────────────

def _bars_from_rows(rows: list[dict[str, Any]]) -> list[Bar]:
    bars: list[Bar] = []
    for row in rows:
        try:
            bar = Bar(
                date=str(row["date"])[:10],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (bar.open, bar.high, bar.low, bar.close)):
            continue
        if bar.close <= 0 or bar.high < bar.low:
            continue
        bars.append(bar)
    bars.sort(key=lambda b: b.date)
    return bars


def fetch_yahoo(ticker: str, lookback_days: int, timeout: float = 20.0) -> list[Bar]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}"
    params = {"range": "2y", "interval": "1d", "includePrePost": "false", "events": "div,splits"}
    response = httpx.get(url, params=params, headers=_UA, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    result = (response.json().get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("Yahoo не повернув даних")
    payload = result[0]
    stamps = payload.get("timestamp") or []
    quote = ((payload.get("indicators") or {}).get("quote") or [{}])[0]
    rows: list[dict[str, Any]] = []
    for idx, stamp in enumerate(stamps):
        day = datetime.fromtimestamp(stamp, tz=ET).date()
        try:
            rows.append(
                {
                    "date": day.isoformat(),
                    "open": quote["open"][idx],
                    "high": quote["high"][idx],
                    "low": quote["low"][idx],
                    "close": quote["close"][idx],
                    "volume": quote["volume"][idx],
                }
            )
        except (KeyError, IndexError, TypeError):
            continue
    bars = _bars_from_rows(rows)

    # якщо сесія ще триває — остання свічка неповна, прибираємо її
    if bars and session_is_open():
        today_et = now_et().date().isoformat()
        if bars[-1].date == today_et:
            bars = bars[:-1]

    if len(bars) < 60:
        raise ValueError(f"Yahoo віддав лише {len(bars)} свічок")
    return bars[-lookback_days:]


def fetch_twelvedata(ticker: str, lookback_days: int, api_key: str, timeout: float = 20.0) -> list[Bar]:
    response = httpx.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": ticker.upper(),
            "interval": "1day",
            "outputsize": min(lookback_days, 5000),
            "order": "ASC",
            "apikey": api_key,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") == "error" or "values" not in payload:
        raise ValueError(f"Twelve Data: {payload.get('message', 'невідома помилка')}")
    rows = [
        {
            "date": item.get("datetime"), "open": item.get("open"), "high": item.get("high"),
            "low": item.get("low"), "close": item.get("close"), "volume": item.get("volume"),
        }
        for item in payload["values"]
    ]
    bars = _bars_from_rows(rows)
    if len(bars) < 60:
        raise ValueError(f"Twelve Data віддав лише {len(bars)} свічок")
    return bars[-lookback_days:]


def fetch_alphavantage(ticker: str, lookback_days: int, api_key: str, timeout: float = 25.0) -> list[Bar]:
    response = httpx.get(
        "https://www.alphavantage.co/query",
        params={
            "function": "TIME_SERIES_DAILY",
            "symbol": ticker.upper(),
            "outputsize": "full",
            "apikey": api_key,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    series = payload.get("Time Series (Daily)")
    if not series:
        note = payload.get("Note") or payload.get("Information") or payload.get("Error Message") or "порожня відповідь"
        raise ValueError(f"Alpha Vantage: {note}")
    rows = [
        {
            "date": day, "open": values.get("1. open"), "high": values.get("2. high"),
            "low": values.get("3. low"), "close": values.get("4. close"), "volume": values.get("5. volume"),
        }
        for day, values in series.items()
    ]
    bars = _bars_from_rows(rows)
    if len(bars) < 60:
        raise ValueError(f"Alpha Vantage віддав лише {len(bars)} свічок")
    return bars[-lookback_days:]


def synthetic_bars(ticker: str, lookback_days: int) -> list[Bar]:
    """Детермінований ряд для офлайн-демо. Позначається як synthetic — не для реальних рішень."""
    seed = int(hashlib.sha256(f"synthetic|{ticker.upper()}".encode()).hexdigest()[:10], 16)
    rng = random.Random(seed)
    price = _BASE_PRICES.get(ticker.upper(), 120.0) * rng.uniform(0.75, 0.95)
    drift = rng.uniform(-0.0006, 0.0011)
    day = now_et().date() - timedelta(days=int(lookback_days * 1.45))
    bars: list[Bar] = []
    while len(bars) < lookback_days:
        if day.weekday() < 5:
            shock = rng.gauss(drift, 0.016)
            open_px = price * (1 + rng.gauss(0, 0.004))
            close_px = max(1.0, price * (1 + shock))
            high_px = max(open_px, close_px) * (1 + abs(rng.gauss(0, 0.006)))
            low_px = min(open_px, close_px) * (1 - abs(rng.gauss(0, 0.006)))
            bars.append(
                Bar(
                    date=day.isoformat(),
                    open=round(open_px, 2), high=round(high_px, 2),
                    low=round(low_px, 2), close=round(close_px, 2),
                    volume=float(int(rng.uniform(20, 120) * 1_000_000)),
                )
            )
            price = close_px
        day += timedelta(days=1)
    return bars


# ─── головна функція ──────────────────────────────────────────────────────────

def fetch_daily(ticker: str, settings, *, force_refresh: bool = False) -> MarketData:
    cfg = settings.data_cfg
    ticker = ticker.upper()
    lookback = int(cfg.get("lookback_days", 420))
    ttl_min = int(cfg.get("cache_ttl_min", 240))
    sources = [str(s).lower() for s in (cfg.get("sources") or ["yahoo"])]
    warnings: list[str] = []

    if not force_refresh:
        cached = _read_cache(settings.cache_dir, ticker, ttl_min)
        if cached and len(cached.get("bars", [])) >= 60:
            cached_bars = completed_sessions_only(_bars_from_rows(cached["bars"]))
            if len(cached_bars) < 60:
                cached = None
            else:
                return MarketData(
                    ticker=ticker,
                    bars=cached_bars,
                    source=f"{cached.get('source', 'cache')} (кеш)",
                    fetched_at=cached["fetched_at"],
                    synthetic=bool(cached.get("synthetic")),
                    warnings=["дані з кеша"],
                )

    errors: list[str] = []
    for provider in sources:
        try:
            if provider == "yahoo":
                bars = fetch_yahoo(ticker, lookback)
            elif provider == "twelvedata":
                key = (settings.env_or("TWELVEDATA_API_KEY") or "").strip()
                if not key:
                    errors.append("twelvedata: немає TWELVEDATA_API_KEY")
                    continue
                bars = fetch_twelvedata(ticker, lookback, key)
            elif provider == "alphavantage":
                key = (settings.env_or("ALPHA_VANTAGE_API_KEY") or "").strip()
                if not key:
                    errors.append("alphavantage: немає ALPHA_VANTAGE_API_KEY")
                    continue
                bars = fetch_alphavantage(ticker, lookback, key)
            elif provider == "synthetic":
                bars = completed_sessions_only(synthetic_bars(ticker, lookback))
                warnings.append("ДЕМО-ДАНІ (синтетичний ряд) — не для реальних рішень")
                payload = MarketData(ticker, bars, "synthetic", datetime.now(timezone.utc).isoformat(timespec="seconds"), True, warnings)
                _write_cache(settings.cache_dir, ticker, payload.to_dict() | {"bars": [b.to_dict() for b in bars]})
                return payload
            else:
                errors.append(f"{provider}: невідоме джерело")
                continue

            bars = completed_sessions_only(bars)
            if len(bars) < 60:
                raise ValueError(f"після відсіву незакритої сесії лишилось {len(bars)} свічок")
            payload = MarketData(
                ticker=ticker, bars=bars, source=provider,
                fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                synthetic=False, warnings=warnings,
            )
            _write_cache(settings.cache_dir, ticker, payload.to_dict())
            return payload
        except Exception as exc:  # noqa: BLE001 — пробуємо наступне джерело
            errors.append(f"{provider}: {type(exc).__name__}: {exc}")
            log.warning("джерело %s не спрацювало для %s: %s", provider, ticker, exc)

    if str(cfg.get("offline_fallback", "synthetic")).lower() == "synthetic":
        warnings.append("Усі джерела недоступні — показую ДЕМО-ДАНІ (синтетика). Не для реальних рішень!")
        warnings.extend(errors)
        bars = synthetic_bars(ticker, lookback)
        return MarketData(ticker, bars, "synthetic", datetime.now(timezone.utc).isoformat(timespec="seconds"), True, warnings)

    raise RuntimeError("Не вдалося отримати добові свічки: " + "; ".join(errors))


def premarket_quote(ticker: str, timeout: float = 15.0) -> dict[str, Any] | None:
    """Остання ціна до відкриття (04:00–09:29 ET) — щоб оцінити нічний ґеп.

    Використовує хвилинні бари Yahoo з includePrePost=true. Якщо премаркету немає
    (напр. вихідний), повертає None — план будується без ґеп-перевірки.
    """
    try:
        response = httpx.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}",
            params={"range": "1d", "interval": "1m", "includePrePost": "true"},
            headers=_UA,
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        result = (response.json().get("chart") or {}).get("result") or []
        if not result:
            return None
        payload = result[0]
        meta = payload.get("meta") or {}
        stamps = payload.get("timestamp") or []
        closes = ((payload.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []

        pre: list[tuple[datetime, float]] = []
        for stamp, close in zip(stamps, closes):
            if close is None:
                continue
            moment = datetime.fromtimestamp(stamp, tz=ET)
            if moment.weekday() < 5 and moment.time() < OPEN_ET:
                pre.append((moment, float(close)))

        previous_close = meta.get("chartPreviousClose") or meta.get("previousClose") or meta.get("regularMarketPrice")
        if pre:
            moment, price = max(pre, key=lambda item: item[0])
            return {
                "price": round(price, 4),
                "as_of": moment.isoformat(timespec="minutes"),
                "session_date": moment.date().isoformat(),
                "previous_close": float(previous_close) if previous_close else None,
                "source": "yahoo-1m-premarket",
            }
        if meta.get("preMarketPrice"):
            return {
                "price": float(meta["preMarketPrice"]),
                "as_of": now_et().isoformat(timespec="minutes"),
                "session_date": now_et().date().isoformat(),
                "previous_close": float(previous_close) if previous_close else None,
                "source": "yahoo-meta-premarket",
            }
        return None
    except Exception as exc:  # noqa: BLE001 — без премаркету план лишається валідним
        log.warning("премаркет %s недоступний: %s", ticker, exc)
        return None


def last_session_date(bars: list[Bar]) -> date_cls:
    return date_cls.fromisoformat(bars[-1].date)


def next_session_date(bars: list[Bar]) -> date_cls:
    """Наступна торгова сесія після останньої свічки (з урахуванням свят NYSE)."""
    return next_trading_day(last_session_date(bars))
