"""Фундаментальні дані без ключів: SEC EDGAR + календар звітностей Nasdaq.

Що беремо:
  • SEC EDGAR — виручка, прибуток, EPS, маржа за кварталами (XBRL-факти) і
    список останніх звітів (10-Q/10-K) та подій (8-K). Ключ не потрібен,
    SEC вимагає лише User-Agent із контактом (SEC_EDGAR_USER_AGENT).
  • Nasdaq — дата найближчого звіту (це найризиковаша подія для входу).

Логіка навмисно детермінована: код рахує зростання виручки/EPS та маржу,
а висновок («звітність за 2 дні — нові входи заборонено») формулює правило,
а не модель. Мережа може бути недоступною — тоді поля лишаються порожніми,
а решта плану працює як раніше.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("assistant.fundamentals")

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings?date={day}"

REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
EXTRA_TAGS = ("NetIncomeLoss", "GrossProfit")

QUARTER_FORMS = ("10-Q", "10-K")
CACHE_TTL_DAYS = 1


# ─── чиста логіка (її й тестуємо) ────────────────────────────────────────────

def _quarterly(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Лишає лише квартальні значення зі звітів і прибирає дублікати.

    У XBRL один і той самий квартал трапляється кілька разів (10-Q, потім 10-K,
    потім виправлення), тому беремо періоди тривалістю 80–100 днів і залишаємо
    останнє за датою подання значення для кожного періоду.
    """
    by_period: dict[tuple[str, str], dict[str, Any]] = {}
    for item in units:
        if item.get("form") not in QUARTER_FORMS or not item.get("start") or not item.get("end"):
            continue
        try:
            days = (date_cls.fromisoformat(item["end"]) - date_cls.fromisoformat(item["start"])).days
        except ValueError:
            continue
        if not 80 <= days <= 100:
            continue
        key = (item["start"], item["end"])
        previous = by_period.get(key)
        if previous is None or str(item.get("filed", "")) > str(previous.get("filed", "")):
            by_period[key] = item
    return sorted(by_period.values(), key=lambda item: (item["end"], item.get("filed", "")))


def series_stats(values: list[float]) -> dict[str, Any]:
    """Останнє значення, попереднє та зміна у відсотках (0 → None, без ділення на нуль)."""
    if not values:
        return {"last": None, "prev": None, "change_pct": None}
    last = values[-1]
    prev = values[-2] if len(values) > 1 else None
    if prev in (None, 0):
        return {"last": last, "prev": prev, "change_pct": None}
    return {"last": last, "prev": prev, "change_pct": round((last - prev) / abs(prev) * 100, 2)}


def yoy_change(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Порівняння останнього кварталу з тим самим кварталом рік тому.

    Саме YoY (а не квартал до кварталу) показує, чи зростає бізнес: у більшості
    компаній квартали мають сильну сезонність.
    """
    if not series:
        return {"last": None, "year_ago": None, "yoy_pct": None, "period": None, "quarters": 0}
    last = series[-1]
    last_end = date_cls.fromisoformat(last["end"])
    year_ago = None
    for item in reversed(series[:-1]):
        gap = abs((last_end - date_cls.fromisoformat(item["end"])).days - 365)
        if gap <= 45:
            year_ago = item
            break
    yoy = None
    if year_ago and year_ago["val"]:
        yoy = round((last["val"] - year_ago["val"]) / abs(year_ago["val"]) * 100, 2)
    return {
        "last": last["val"],
        "period": last["end"],
        "year_ago": year_ago["val"] if year_ago else None,
        "yoy_pct": yoy,
        "quarters": len(series),
    }


def latest_end(series: list[dict[str, Any]]) -> str:
    """Дата кінця найсвіжішого кварталу в серії (порожній рядок, якщо серії немає)."""
    return str(series[-1]["end"]) if series else ""


def value_at(series: list[dict[str, Any]], period: str | None) -> float | None:
    """Значення саме за той квартал, що й `period` (щоб не ділити прибуток одного кварталу
    на виручку іншого — саме так народжуються «маржі» у 1900%)."""
    if not period:
        return None
    for item in series:
        if str(item.get("end")) == str(period):
            return item.get("val")
    return None


def margin(numerator: float | None, revenue: float | None) -> float | None:
    if not revenue or numerator is None:
        return None
    value = numerator / revenue * 100
    if not -200 <= value <= 200:          # >200% — це вже не маржа, а збій відповідності періодів
        return None
    return round(value, 2)


def recent_filings(filings: list[dict[str, Any]], today: date_cls, *, months: int = 6) -> dict[str, Any]:
    """Свіжість звітності: коли був останній 10-Q/10-K і скільки 8-K за місяць."""
    horizon = today - timedelta(days=30 * months)
    relevant = [
        {
            "form": str(item.get("form")),
            "date": str(item.get("filingDate")),
            "url": item.get("primaryDocUrl") or item.get("url"),
        }
        for item in filings
        if str(item.get("form")) in ("10-Q", "10-K", "8-K")
        and str(item.get("filingDate", "")) >= horizon.isoformat()
    ]
    last_report = next((f for f in relevant if f["form"] in ("10-Q", "10-K")), None)
    eight_k = [f for f in relevant if f["form"] == "8-K"
               and f["date"] >= (today - timedelta(days=30)).isoformat()]
    days_since = None
    if last_report:
        try:
            days_since = (today - date_cls.fromisoformat(last_report["date"])).days
        except ValueError:
            days_since = None
    return {
        "last_report": last_report,
        "days_since_report": days_since,
        "eight_k_30d": len(eight_k),
        "recent": relevant[:6],
    }


def next_report_estimate(report_dates: list[str], last_report: str | None, today: date_cls) -> dict[str, Any] | None:
    """Оцінка дати наступного звіту за каденцією подань 10-Q/10-K (без Nasdaq).

    Компанії звітують ритмічно: медіанний інтервал між квартальними звітами
    (типово 90–92 дні) переносимо на останній відомий звіт. Це оцінка, не факт,
    тому вона позначена `source: "sec_estimate"`, а причина блокування прямо
    каже, що дата приблизна.
    """
    if last_report:
        try:
            anchor = date_cls.fromisoformat(str(last_report))
        except ValueError:
            anchor = None
    else:
        anchor = None
    if anchor is None and report_dates:
        try:
            anchor = max(date_cls.fromisoformat(str(d)) for d in report_dates)
        except ValueError:
            anchor = None
    if anchor is None:
        return None

    gaps: list[int] = []
    parsed = sorted({str(d) for d in report_dates})
    for previous, current in zip(parsed, parsed[1:]):
        try:
            delta = (date_cls.fromisoformat(current) - date_cls.fromisoformat(previous)).days
        except ValueError:
            continue
        if 60 <= delta <= 130:                      # квартальний ритм, а не річний звіт
            gaps.append(delta)
    gap = sorted(gaps)[len(gaps) // 2] if gaps else 91
    expected = anchor + timedelta(days=gap)
    return {
        "date": expected.isoformat(),
        "days_to": (expected - today).days,
        "timing": "",
        "source": "sec_estimate",
        "note": f"оцінка за каденцією звітів SEC ({gap} дн. між подачами), не підтверджена біржа",
    }


def earnings_from_calendar(days: dict[str, list[dict[str, Any]]], ticker: str, today: date_cls) -> dict[str, Any] | None:
    """Знаходить найближчий звіт тікера в календарі Nasdaq (по днях)."""
    for day in sorted(days):
        for row in days[day]:
            if str(row.get("symbol", "")).upper() != ticker.upper():
                continue
            try:
                when = date_cls.fromisoformat(day)
            except ValueError:
                continue
            return {
                "date": day,
                "days_to": (when - today).days,
                "timing": row.get("time") or "",
                "eps_forecast": row.get("epsForecast") or "",
                "name": row.get("name") or "",
            }
    return None


def build_packet(
    *,
    ticker: str,
    company: str | None,
    revenue: list[dict[str, Any]],
    eps: list[dict[str, Any]],
    net_income: list[dict[str, Any]],
    gross_profit: list[dict[str, Any]],
    filings: dict[str, Any],
    earnings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Збирає фундаментальний паспорт і робить детермінований висновок."""
    rev = yoy_change(revenue)
    eps_yoy = yoy_change(eps)
    packet: dict[str, Any] = {
        "company": company,
        "revenue": rev,
        "eps": eps_yoy,
        "net_margin_pct": margin(value_at(net_income, rev["period"]), rev["last"]),
        "gross_margin_pct": margin(value_at(gross_profit, rev["period"]), rev["last"]),
        "filings": filings,
        "earnings": earnings,
        "notes": [],
        "score": 0,
    }

    notes: list[str] = []
    score = 0

    if rev["yoy_pct"] is not None:
        if rev["yoy_pct"] > 10:
            notes.append(f"виручка зростає: {rev['yoy_pct']:+.1f}% рік до року")
            score += 1
        elif rev["yoy_pct"] < 0:
            notes.append(f"виручка падає: {rev['yoy_pct']:+.1f}% рік до року")
            score -= 1
        else:
            notes.append(f"виручка майже без змін: {rev['yoy_pct']:+.1f}% рік до року")

    if eps_yoy["yoy_pct"] is not None:
        if eps_yoy["yoy_pct"] > 10:
            notes.append(f"прибуток на акцію зростає: {eps_yoy['yoy_pct']:+.1f}% рік до року")
            score += 1
        elif eps_yoy["yoy_pct"] < 0:
            notes.append(f"прибуток на акцію падає: {eps_yoy['yoy_pct']:+.1f}% рік до року")
            score -= 1

    if packet["net_margin_pct"] is not None:
        if packet["net_margin_pct"] < 0:
            notes.append(f"останній квартал збитковий (маржа {packet['net_margin_pct']:.1f}%)")
            score -= 1
        else:
            notes.append(f"чиста маржа {packet['net_margin_pct']:.1f}%")

    if earnings is None:
        last_report = filings.get("last_report")
        if isinstance(last_report, dict):
            last_report = last_report.get("date")
        estimate = next_report_estimate(
            filings.get("report_dates") or [], last_report, datetime.now().date()
        )
        if estimate:
            earnings = estimate
            packet["earnings"] = estimate      # у паспорті має бути саме те, що потім читає глибина

    if earnings:
        days = earnings.get("days_to")
        source = earnings.get("source")
        approx = " (оцінка за каденцією SEC)" if source == "sec_estimate" else ""
        if days is not None:
            if days <= 3:
                notes.append(f"⚠️ звітність за {days} дн. ({earnings['date']}){approx} — високий ризик новин")
                score -= 1
            else:
                notes.append(f"найближча звітність через {days} дн. ({earnings['date']}){approx}")
    elif filings.get("days_since_report") is not None:
        notes.append(f"останній звіт {filings['days_since_report']} дн. тому")

    if filings.get("eight_k_30d"):
        notes.append(f"подієвих звітів 8-K за місяць: {filings['eight_k_30d']}")

    packet["notes"] = notes
    packet["score"] = max(-3, min(3, score))
    return packet


def _freshest(settings, cik: int, tags: tuple[str, ...], *, fresh_days: int = 240) -> list[dict[str, Any]]:
    """Серія з найсвіжішим останнім кварталом серед кількох тегів.

    Зупиняємось достроково, щойно знайдено тег, чий квартал молодший за `fresh_days`
    (типово 8 місяців) — щоб не смикати SEC зайвими запитами.
    """
    best: list[dict[str, Any]] = []
    for tag in tags:
        series = _concept(settings, cik, tag)
        if latest_end(series) > latest_end(best):
            best = series
        if best:
            try:
                age = (datetime.now().date() - date_cls.fromisoformat(latest_end(best))).days
            except ValueError:
                age = 10_000
            if age <= fresh_days:
                break
    return best


# Секундні інтервали між запитами до SEC: у них ліміт 10 запитів/с,
# а ми тепер ходимо паралельно (прогрів кеша по всьому списку).
_SEC_LOCK = threading.Lock()
_SEC_LAST = [0.0]
SEC_MIN_INTERVAL = 0.12


def _throttle() -> None:
    with _SEC_LOCK:
        wait = SEC_MIN_INTERVAL - (time.monotonic() - _SEC_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _SEC_LAST[0] = time.monotonic()


# ─── мережа + кеш ────────────────────────────────────────────────────────────

def _cache_path(cache_dir: Path, name: str) -> Path:
    folder = cache_dir / "sec"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.json"


def _read_cache(cache_dir: Path, name: str, ttl_days: int = CACHE_TTL_DAYS,
                ttl_hours: float | None = None) -> Any | None:
    path = _cache_path(cache_dir, name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        age = datetime.now() - datetime.fromisoformat(payload["fetched_at"])
    except (KeyError, ValueError):
        return None
    ttl = timedelta(hours=ttl_hours) if ttl_hours is not None else timedelta(days=ttl_days)
    if age > ttl:
        return None
    return payload.get("data")


def _write_cache(cache_dir: Path, name: str, data: Any) -> None:
    try:
        _cache_path(cache_dir, name).write_text(
            json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "data": data},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:                                   # noqa: BLE001
        log.debug("не вдалося зберегти кеш %s: %s", name, exc)


def _headers(settings) -> dict[str, str]:
    contact = (settings.env_or("SEC_EDGAR_USER_AGENT", "") or "TradingAssistant research@example.com").strip()
    return {"User-Agent": contact, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}


def ticker_to_cik(settings, ticker: str) -> int | None:
    """Мапа тікер → CIK від SEC (кеш на 7 днів; файл ~780 КБ)."""
    mapping = _read_cache(settings.cache_dir, "company_tickers", ttl_days=7)
    if mapping is None:
        try:
            headers = {"User-Agent": _headers(settings)["User-Agent"], "Accept-Encoding": "gzip, deflate"}
            response = httpx.get(SEC_TICKERS_URL, headers=headers, timeout=25.0)
            response.raise_for_status()
            mapping = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in response.json().values()}
            _write_cache(settings.cache_dir, "company_tickers", mapping)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("SEC: не вдалося отримати список тікерів (%s)", exc)
            return None
    return mapping.get(ticker.upper())


def _concept(settings, cik: int, tag: str) -> list[dict[str, Any]]:
    cached = _read_cache(settings.cache_dir, f"concept_{cik}_{tag}")
    if cached is not None:
        return cached
    url = SEC_CONCEPT_URL.format(cik=cik, tag=tag)
    try:
        _throttle()
        response = httpx.get(url, headers=_headers(settings), timeout=25.0)
        if response.status_code != 200:
            _write_cache(settings.cache_dir, f"concept_{cik}_{tag}", [])
            return []
        units = response.json().get("units", {})
        rows = units.get("USD") or units.get("USD/shares") or next(iter(units.values()), [])
        quarterly = [
            {"start": row["start"], "end": row["end"], "val": row["val"],
             "form": row.get("form"), "filed": row.get("filed")}
            for row in _quarterly(rows)
        ]
        _write_cache(settings.cache_dir, f"concept_{cik}_{tag}", quarterly)
        return quarterly
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.debug("SEC %s: %s", tag, exc)
        return []


def _filings(settings, cik: int, today: date_cls) -> dict[str, Any]:
    cached = _read_cache(settings.cache_dir, f"filings_{cik}")
    if cached is not None:
        return cached
    url = SEC_SUBMISSIONS_URL.format(cik=cik)
    try:
        _throttle()
        response = httpx.get(url, headers=_headers(settings), timeout=25.0)
        response.raise_for_status()
        payload = response.json()
        recent = payload.get("filings", {}).get("recent", {})
        rows = [
            {"form": recent["form"][i], "filingDate": recent["filingDate"][i],
             "primaryDocUrl": (
                 f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                 f"{str(recent['accessionNumber'][i]).replace('-', '')}/"
                 f"{recent['primaryDocument'][i]}"
             )}
            for i in range(min(len(recent.get("form", [])), 60))
        ]
        result = recent_filings(rows, today)
        result["company"] = payload.get("name")
        # дати саме квартальних/річних звітів — для оцінки каденції (потрібна історія, не 6 міс.)
        result["report_dates"] = [row["filingDate"] for row in rows if row["form"] in QUARTER_FORMS][:8]
        _write_cache(settings.cache_dir, f"filings_{cik}", result)
        return result
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.debug("SEC submissions: %s", exc)
        return {"last_report": None, "days_since_report": None, "eight_k_30d": 0,
                "recent": [], "report_dates": []}


def _earnings_day(settings, day: date_cls) -> tuple[str, list[dict[str, Any]]]:
    """Один день календаря Nasdaq (кеш на добу). Вихідні/свята → порожній список."""
    name = f"earnings_{day.isoformat()}"
    cached = _read_cache(settings.cache_dir, name)
    if cached is not None:
        return day.isoformat(), cached
    # Невдачу (таймаут Nasdaq) теж кешуємо — але лише на 6 годин, щоб прогін
    # у тому ж днищі не витрачав по 6 секунд таймауту на кожен день.
    if _read_cache(settings.cache_dir, f"{name}_fail", ttl_hours=6) is not None:
        return day.isoformat(), []
    url = NASDAQ_EARNINGS_URL.format(day=day.isoformat())
    rows: list[dict[str, Any]] = []
    try:
        # короткий таймаут: календар Nasdaq — бонус, а не основа. Основна оцінка
        # дати звіту будується з подань SEC (вони вже в кеші) — див. next_report_estimate.
        response = httpx.get(url, headers={"User-Agent": _headers(settings)["User-Agent"]}, timeout=6.0)
        if response.status_code == 200:
            rows = ((response.json().get("data") or {}).get("rows")) or []
            _write_cache(settings.cache_dir, name, rows)      # кешуємо й порожні дні (вихідні)
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Nasdaq earnings %s: %s", day, exc)
        _write_cache(settings.cache_dir, f"{name}_fail", {"reason": type(exc).__name__})
    return day.isoformat(), rows


def earnings_calendar(settings, days_ahead: int = 4) -> dict[str, list[dict[str, Any]]]:
    """Календар звітностей Nasdaq на найближчі дні (кеш на добу, по запиту на день).

    Дні тягнемо паралельно: послідовно це було ~13 с, паралельно — близько секунди.
    Порожні відповіді (вихідні) не кешуємо, щоб не «залипали» на добу.
    """
    today = datetime.now().date()
    days = [today + timedelta(days=offset) for offset in range(max(1, days_ahead) + 1)]
    out: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(days))) as pool:
        for key, rows in pool.map(lambda day: _earnings_day(settings, day), days):
            if rows:
                out[key] = rows
    return out


def fetch_fundamentals(settings, ticker: str, *, calendar: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any] | None:
    """Фундаментальний паспорт тікера. None — якщо немає ні SEC, ні кеша (офлайн)."""
    cached = _read_cache(settings.cache_dir, f"packet_{ticker.upper()}")
    if cached is not None:
        return cached

    cik = ticker_to_cik(settings, ticker)
    if cik is None:
        return None

    today = datetime.now().date()
    filings = _filings(settings, cik, today)

    # У XBRL один і той самий показник лежить під кількома тегами, і частина з них
    # «замерзла» на старі роках (компанія змінила тег). Тому беремо не перший
    # непорожній, а той, чий останній квартал найсвіжіший.
    revenue = _freshest(settings, cik, REVENUE_TAGS)
    eps = _freshest(settings, cik, EPS_TAGS)
    net_income = _concept(settings, cik, EXTRA_TAGS[0])
    gross_profit = _concept(settings, cik, EXTRA_TAGS[1])

    calendar = calendar if calendar is not None else earnings_calendar(settings)
    earnings = earnings_from_calendar(calendar, ticker, today)

    packet = build_packet(
        ticker=ticker,
        company=filings.get("company"),
        revenue=revenue, eps=eps, net_income=net_income, gross_profit=gross_profit,
        filings=filings, earnings=earnings,
    )
    if any(packet[key]["last"] for key in ("revenue", "eps")) or filings.get("last_report"):
        _write_cache(settings.cache_dir, f"packet_{ticker.upper()}", packet)
    return packet
