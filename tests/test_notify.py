"""Повідомлення в Telegram: довге зведення по 20 тікерах не має обрізатися.

У Telegram ліміт 4096 символів на повідомлення. Раніше текст просто обрізався
(«text[:4000]») — і останні плани у зведенні губилися б мовчки. Тепер текст
ділиться на частини по рядках.
"""

from __future__ import annotations

from app import notify
from app.db import Store
from app.pipeline import build_proposals


def test_short_message_is_single_part():
    parts = notify.split_message("📊 План на 12.10.2026 — 3 тікерів")
    assert len(parts) == 1


def test_long_message_is_split_without_losing_lines():
    lines = [f"🟢 <b>TICK{i:02d}</b>: ліміт {100 + i}.00 · стоп 95.00 · ціль 120.00 · 4 шт." for i in range(120)]
    text = "\n".join(lines)
    parts = notify.split_message(text)

    assert len(parts) > 1
    assert all(len(part) <= notify.TELEGRAM_LIMIT + 40 for part in parts)   # +40 на підпис «частина n/m»
    rebuilt = "\n".join(part.replace(f"\n<i>(частина {i + 1}/{len(parts)})</i>", "")
                        for i, part in enumerate(parts))
    for line in lines:                        # жоден рядок не загубився й не обрізався
        assert line in rebuilt
    assert "частина 1/" in parts[0]


def test_summary_of_twenty_tickers_reaches_telegram_whole(settings, monkeypatch):
    """Реальний сценарій: 20 тікерів у watchlist → зведення не обрізається."""
    settings.config["watchlist"] = [f"T{i:02d}" for i in range(20)]
    settings.config["schedule"]["enabled"] = False
    store = Store(settings.db_path)

    proposals = build_proposals(settings, store, settings.watchlist,
                                notify=True, use_llm=False, force_refresh=True)
    assert len(proposals) == 20

    text = settings.outbox_path.read_text(encoding="utf-8")
    parts = notify.split_message(text)
    joined = "\n".join(parts)
    for proposal in proposals:                # кожен тікер є в тому, що піде в Telegram
        assert proposal.ticker in joined

    sent: list[str] = []
    def fake_post(url, **kwargs):
        sent.append(kwargs["json"]["text"])
        return _Resp()

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    ok, detail = notify.send_telegram(text, "token", "chat")
    assert ok and detail == "ok"
    assert len(sent) >= 1
    assert all(len(part) <= notify.TELEGRAM_LIMIT + 40 for part in sent)


class _Resp:
    status_code = 200
    text = "ok"
