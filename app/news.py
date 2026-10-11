"""Новини по тікеру без ключа: стрічка Yahoo Finance (RSS).

Дає заголовки, посилання й час. Нічого не інтерпретує: оцінку тональності
(якщо потрібна) робить LLM окремо, і вона не впливає на рівні чи вердикт гейта.
Кеш — 90 хвилин, щоб не смикати стрічку на кожен прогін.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

log = logging.getLogger("assistant.news")

FEED_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
TTL_MINUTES = 90


def parse_rss(xml_text: str, limit: int = 6) -> list[dict[str, Any]]:
    """Заголовки зі стрічки RSS (чиста функція — саме її перевіряють тести)."""
    items: list[dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return items
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        published = (node.findtext("pubDate") or "").strip()
        when = None
        if published:
            try:
                when = parsedate_to_datetime(published).astimezone().isoformat(timespec="minutes")
            except (TypeError, ValueError):
                when = None
        items.append({
            "title": title,
            "link": (node.findtext("link") or "").strip(),
            "published": when,
        })
        if len(items) >= limit:
            break
    return items


def _cache_path(cache_dir: Path, ticker: str) -> Path:
    folder = cache_dir / "news"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{ticker.upper()}.json"


def headlines(ticker: str, settings, *, limit: int = 6) -> list[dict[str, Any]]:
    """Останні заголовки. Порожній список — якщо мережі немає (це не помилка плану)."""
    path = _cache_path(settings.cache_dir, ticker)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age = datetime.now() - datetime.fromisoformat(payload["fetched_at"])
            if age < timedelta(minutes=TTL_MINUTES):
                return payload.get("items", [])[:limit]
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass

    try:
        response = httpx.get(
            FEED_URL.format(ticker=ticker.upper()),
            headers={"User-Agent": "Mozilla/5.0 (compatible; TradingAssistant/0.3)"},
            timeout=15.0,
        )
        response.raise_for_status()
        items = parse_rss(response.text, limit=limit)
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("новини %s: %s", ticker, exc)
        return []

    try:
        path.write_text(
            json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "items": items},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return items
