"""Ціна на момент перевірки: премаркет, регулярна сесія, після закриття або остання сесія.

Навіщо окремий модуль: план будується на **закритих** добових свічках (це принципово —
інакше рівні «пливуть»), але користувачеві потрібно бачити, скільки ця акція коштує
**зараз**, включно з позасесійною торгівлею. Ці два числа різні за визначенням, і раніше
на сторінці було лише перше — звідси враження «дані не збігаються з реальністю».

Джерела (усі безкоштовні, без ключів):
  1. Yahoo chart 1m + includePrePost — хвилинні ціни премаркету (04:00–09:30 ET),
     основної сесії (09:30–16:00) і після закриття (16:00–20:00);
  2. Yahoo meta (regularMarketPrice + regularMarketTime) — коли хвилинних барів немає
     (вихідний, свято, до 04:00 ET);
  3. Nasdaq info (lastSalePrice + lastTradeTimestamp) — страховка, якщо Yahoo мовчить.

Кеш — 5 хвилин: прогін робить один запит на тікер, повторний запуск того ж ранку
не смикає джерело.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("assistant.quote")

ET = ZoneInfo("America/New_York")
PRE_OPEN_ET = time(4, 0)
OPEN_ET = time(9, 30)
CLOSE_ET = time(16, 0)
AFTER_HOURS_END_ET = time(20, 0)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; TradingAssistant/0.2; personal research)"}
QUOTE_TTL_MIN = 5

SESSION_PRE = "премаркет"
SESSION_REGULAR = "сесія"
SESSION_AFTER = "після закриття"
SESSION_LAST = "остання сесія"


def session_of(moment: datetime) -> str:
    """До якої частини торгового дня належить момент (у таймзоні біржі)."""
    clock = moment.astimezone(ET).time()
    if clock < PRE_OPEN_ET:
        return SESSION_LAST
    if clock < OPEN_ET:
        return SESSION_PRE
    if clock <= CLOSE_ET:
        return SESSION_REGULAR          # 16:00 — це друк закриття, а не післясесійна торгівля
    if clock < AFTER_HOURS_END_ET:
        return SESSION_AFTER
    return SESSION_LAST


def _cache_path(cache_dir: Path, ticker: str) -> Path:
    folder = cache_dir / "quotes"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{ticker.upper()}.json"


def _payload(price: float, *, prev_close: float | None, as_of: datetime,
             session: str, source: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(ET)
    as_of = as_of.astimezone(ET)
    stale = as_of.date() != now.date()
    if stale:
        # Ціна не за сьогодні (вихідний, свято, до 04:00 ET) — для користувача це
        # «закриття попередньої сесії», який би час не мав останній хвилинний бар.
        session = SESSION_LAST
    return {
        "price": round(float(price), 4),
        "prev_close": round(float(prev_close), 4) if prev_close else None,
        "as_of": as_of.isoformat(timespec="minutes"),
        "as_of_label": f"{as_of:%H:%M} ET {as_of:%d.%m}",
        "label": (f"закриття {as_of:%d.%m}" if session == SESSION_LAST
                  else f"{session} {as_of:%H:%M} ET"),
        "session": session,
        "session_date": as_of.date().isoformat(),
        "source": source,
        "stale": stale,
        "delay_min": max(0, int((now - as_of).total_seconds() // 60)),
    }


# ─── джерела ─────────────────────────────────────────────────────────────────

def _from_yahoo_minutes(ticker: str, timeout: float) -> dict[str, Any] | None:
    """Остання хвилинна ціна з пре/постмаркетом (yahoo-1m)."""
    response = httpx.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}",
        params={"range": "1d", "interval": "1m", "includePrePost": "true"},
        headers=_UA, timeout=timeout, follow_redirects=True,
    )
    response.raise_for_status()
    results = (response.json().get("chart") or {}).get("result") or []
    if not results:
        return None
    payload = results[0]
    meta = payload.get("meta") or {}
    stamps = payload.get("timestamp") or []
    closes = ((payload.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []

    latest: tuple[datetime, float] | None = None
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        moment = datetime.fromtimestamp(stamp, tz=ET)
        if moment.weekday() >= 5 and session_of(moment) == SESSION_PRE:
            continue                                   # «недільний премаркет» ігноруємо
        if latest is None or moment > latest[0]:
            latest = (moment, float(close))

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    today = datetime.now(ET).date()
    if latest is None or latest[0].date() != today:
        # Ринок закритий (вихідний, свято, до 04:00 ET). Хвилинний ряд тоді закінчується
        # післясесійним принтом (19:59 ET), а брокер показує офіційне закриття — тому
        # беремо саме meta.regularMarketPrice: інакше виникає «розбіжність» на 0.05–0.2%.
        price = meta.get("regularMarketPrice") or (latest[1] if latest else None)
        if not price:
            return None
        moment = datetime.now(ET)
        stamp = meta.get("regularMarketTime") or (latest[0].timestamp() if latest else None)
        if stamp:
            moment = datetime.fromtimestamp(stamp, tz=ET)
        if moment.date() == today and latest is not None:
            moment = latest[0]                    # свіжий післясесійний принт — він точніший
            price = latest[1]
        return _payload(float(price), prev_close=prev_close, as_of=moment,
                        session=session_of(moment), source="yahoo-meta")
    moment, price = latest
    return _payload(price, prev_close=prev_close, as_of=moment,
                    session=session_of(moment), source="yahoo-1m")


def _from_nasdaq(ticker: str, timeout: float) -> dict[str, Any] | None:
    """Страховка: Nasdaq віддає останню ціну й дату навіть коли Yahoo недоступний."""
    response = httpx.get(
        f"https://api.nasdaq.com/api/quote/{ticker.upper()}/info",
        params={"assetclass": "stocks"}, headers=_UA, timeout=timeout,
    )
    response.raise_for_status()
    data = (response.json() or {}).get("data") or {}
    primary = data.get("primaryData") or {}
    raw = str(primary.get("lastSalePrice") or "").replace("$", "").replace(",", "").strip()
    if not raw or raw.upper() == "N/A":
        return None
    try:
        price = float(raw)
    except ValueError:
        return None
    moment = datetime.now(ET)
    stamp = str(primary.get("lastTradeTimestamp") or "")
    for fmt in ("%b %d, %Y %I:%M %p ET", "%b %d, %Y", "%b %d, %Y %H:%M"):
        try:
            moment = datetime.strptime(stamp, fmt).replace(tzinfo=ET)
            break
        except ValueError:
            continue
    return _payload(price, prev_close=None, as_of=moment, session=SESSION_LAST, source="nasdaq")


# ─── публічний API ───────────────────────────────────────────────────────────

def live_quote(ticker: str, settings=None, *, force_refresh: bool = False,
               timeout: float = 15.0) -> dict[str, Any] | None:
    """Ціна тікера на момент перевірки. None — якщо джерела недоступні (це не помилка)."""
    cache_dir: Path | None = None
    if settings is not None:
        cache_dir = settings.cache_dir
        if not force_refresh:
            cached = read_cached(cache_dir, ticker)
            if cached is not None:
                return cached

    quote: dict[str, Any] | None = None
    for fetch in (_from_yahoo_minutes, _from_nasdaq):
        try:
            quote = fetch(ticker, timeout)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.debug("котирування %s через %s: %s", ticker, fetch.__name__, exc)
            continue
        if quote:
            break

    if quote and cache_dir is not None:
        write_cached(cache_dir, ticker, quote)
    return quote


def read_cached(cache_dir: Path, ticker: str) -> dict[str, Any] | None:
    path = _cache_path(cache_dir, ticker)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(payload["fetched_at"])).total_seconds() / 60
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return None
    if age_min > QUOTE_TTL_MIN:
        return None
    return payload.get("data")


def write_cached(cache_dir: Path, ticker: str, quote: dict[str, Any]) -> None:
    try:
        _cache_path(cache_dir, ticker).write_text(
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "data": quote}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("кеш котирування %s не збережено: %s", ticker, exc)


def attach(proposal, settings, *, force_refresh: bool = False) -> dict[str, Any] | None:
    """Проставляє ціну на момент перевірки у пропозицію (для будь-якого вердикту).

    Важливо: це суто інформаційне поле. Рівні входу/стопу/цілі, розмір і вердикт
    далі рахуються від **закритих** свічок — інакше план «пливе» на кожному тику.
    """
    quote = live_quote(proposal.ticker, settings, force_refresh=force_refresh)
    if not quote:
        return None
    proposal.live_price = quote["price"]
    proposal.live_as_of = quote["as_of"]
    proposal.live_session = quote["session"]
    proposal.live_source = quote["source"]
    proposal.live_label = f'{quote["session"]} · {quote["as_of_label"]}'
    if proposal.last_close:
        proposal.live_vs_close_pct = round((quote["price"] - proposal.last_close) / proposal.last_close * 100, 2)
    if proposal.entry_price:
        proposal.live_vs_entry_pct = round((quote["price"] - proposal.entry_price) / proposal.entry_price * 100, 2)
    return quote
