"""Сповіщення: Telegram + локальний outbox-лог (щоб нічого не губилося)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .models import RunRecord
from .risk import Book


def _fmt_money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "—"


_WEEKDAYS_UK = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


def _session_label(session: str) -> str:
    """'2026-10-12' → '12.10.2026 (пн)'"""
    try:
        day = datetime.fromisoformat(session).date()
    except ValueError:
        return session
    return f"{day.day:02d}.{day.month:02d}.{day.year} ({_WEEKDAYS_UK[day.weekday()]})"


def _open_line(for_session: str) -> str:
    """Коли відкривається сесія, і скільки до неї лишилось (для pre-open прогону)."""
    from . import market as market_module

    now = market_module.now_et()
    try:
        target_date = datetime.fromisoformat(for_session).date()
    except ValueError:
        return ""

    if target_date == now.date() and now.time() < market_module.OPEN_ET:
        minutes = market_module.minutes_to_open(now)
        return f"⏱ Відкриття US-ринку сьогодні о 09:30 ET — залишилось {minutes} хв"
    if target_date == now.date():
        return "⏱ Сесія вже торгується (09:30–16:00 ET)"
    return f"⏱ Відкриття US-ринку {_session_label(for_session)} о 09:30 ET"


def format_proposal(proposal) -> str:
    """Повідомлення про пропозицію на наступну сесію (Telegram HTML)."""
    head = f"📅 Пропозиція на {_session_label(proposal.for_session)}"
    badge = {"BUY": "🟢 BUY", "SELL_WARN": "🔴 ЗМЕНШИТИ", "WAIT": "⚪️ ПОЗА РИНКОМ"}.get(proposal.bias, proposal.bias)
    lines = [head, f"{badge} · <b>{proposal.ticker}</b>"]

    if proposal.synthetic:
        lines.append("⚠️ ДЕМО-ДАНІ (синтетичний ряд) — не для реальних рішень")
    open_line = _open_line(proposal.for_session)
    if open_line:
        lines.append(open_line)
    lines.append(f"Остання закрита сесія {proposal.last_bar_date}: закриття ${proposal.last_close:,.2f} ({proposal.change_pct:+.2f}%)")
    if proposal.premarket_price:
        lines.append(
            f"Премаркет ${proposal.premarket_price:,.2f} ({proposal.premarket_gap_pct or 0:+.2f}%) — {proposal.premarket_as_of or ''}"
        )

    if proposal.bias == "BUY" and proposal.entry_price:
        kind = "ринок на відкритті" if proposal.entry_type == "market" else "ліміт"
        lines.append(
            f"План ({kind}): вхід <b>${proposal.entry_price:,.2f}</b> → стоп ${proposal.stop_price:,.2f} → "
            f"ціль ${proposal.target_price:,.2f} · R:R {proposal.rr}"
        )
        if proposal.qty:
            lines.append(
                f"Розмір: <b>{proposal.qty} шт.</b> ≈ {_fmt_money(proposal.notional)} · "
                f"ризик {_fmt_money(proposal.risk_usd)} ({proposal.risk_pct_of_equity or 0:.2f}% капіталу)"
            )
        lines.append(f"Впевненість {proposal.confidence:.2f} · " + ("✅ схвалено ризик-гейтом" if proposal.approved else "⛔ відхилено"))
    else:
        lines.append("Сетапу немає: " + " ".join(proposal.reasons[:2]))

    for reason in proposal.reasons if proposal.bias != "BUY" else proposal.gate_reasons:
        lines.append(f"   • {reason}")
    if proposal.premarket_note:
        lines.append(f"🌅 {proposal.premarket_note}")
    if proposal.invalidation:
        lines.append(f"🚫 {proposal.invalidation}")
    if proposal.note:
        snippet = proposal.note.strip()
        lines.append(f"\n🧠 {snippet[:700]}{'…' if len(snippet) > 700 else ''}")
    if proposal.order_template:
        lines.append(f"\n📋 <code>{proposal.order_template}</code>")
    return "\n".join(lines)


def deliver_proposal(proposal, settings, *, always: bool = False) -> bool:
    """Надсилає пропозицію в Telegram (якщо налаштовано) і пише в outbox."""
    text = format_proposal(proposal)
    only_actionable = bool(settings.notify_cfg.get("only_actionable", True))
    skip = not always and only_actionable and not proposal.actionable
    _append_outbox(settings.outbox_path, text, skipped=skip)
    if skip:
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not settings.notify_cfg.get("telegram", True) or not token or not chat_id:
        return False
    ok, detail = send_telegram(text, token, chat_id)
    _append_outbox(settings.outbox_path, f"[telegram] {detail}", skipped=False)
    return ok


def deliver_proposals_summary(proposals, settings, session: str) -> bool:
    """Одна збірка на всю сесію: що робимо завтра і що ігноруємо."""
    if not proposals:
        return False
    actionable = [p for p in proposals if p.actionable]
    waiting = [p for p in proposals if p.bias == "WAIT"]
    open_line = _open_line(session)
    lines = [
        f"📊 <b>План на {_session_label(session)}</b> — {len(proposals)} тікерів",
        *([open_line] if open_line else []),
        f"✅ з планом: {len(actionable)} · ⚪️ без сетапу: {len(waiting)}",
        "",
    ]
    for p in proposals:
        if p.actionable:
            kind = "ринок" if p.entry_type == "market" else f"ліміт {p.entry_price:,.2f}"
            lines.append(
                f"🟢 <b>{p.ticker}</b>: {kind} · стоп {p.stop_price:,.2f} · ціль {p.target_price:,.2f} · "
                f"{p.qty} шт. · ризик {p.risk_pct_of_equity or 0:.2f}%"
            )
        elif p.bias == "WAIT":
            lines.append(f"⚪️ <b>{p.ticker}</b>: без сетапу ({p.reasons[0][:80] if p.reasons else '—'})")
    lines += ["", "Рішення за вами — ордери тут не ставляться. Повні графіки — в дашборді."]
    text = "\n".join(lines)
    _append_outbox(settings.outbox_path, text, skipped=False)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not settings.notify_cfg.get("telegram", True) or not token or not chat_id:
        return False
    ok, _ = send_telegram(text, token, chat_id)
    return ok


def format_record(record: RunRecord, book: Book | None = None) -> str:
    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪️"}.get(record.action or "", "❔")
    head = f"{emoji} <b>{record.ticker}</b> · {record.analysis_date} · {record.engine}"
    if record.status == "error":
        return f"{head}\n❌ Помилка аналізу: {record.error or 'невідомо'}"

    lines = [
        head,
        f"Рішення: <b>{record.action}</b>"
        + (f" ({record.rating})" if record.rating else "")
        + f" · впевненість {record.confidence or 0:.2f}",
    ]
    if record.reference_price:
        lines.append(f"Ціна: ${record.reference_price:,.2f}")

    if record.kill_switch:
        lines.append("🛑 <b>KILL-SWITCH активний</b> — торгівля зупинена на сьогодні")
    elif record.approved:
        lines.append(f"✅ Дозволено ризик-гейтом: {record.quantity} шт. ≈ {_fmt_money(record.notional)}")
        if record.stop_loss_price:
            lines.append(f"Стоп ${record.stop_loss_price:,.2f} · тейк ${record.take_profit_price:,.2f}")
    else:
        lines.append("⛔ Відхилено ризик-гейтом")

    for reason in [r for r in (record.reasons or "").split("\n") if r.strip()]:
        lines.append(f"   • {reason}")

    if book is not None:
        lines.append(f"Рахунок: equity {_fmt_money(book.equity)} · кеш {_fmt_money(book.cash)} · P/L {book.daily_pnl_pct():+.2f}%")

    if record.rationale:
        snippet = record.rationale.replace("[MOCK] ", "").strip()
        lines.append(f"\n📝 {snippet[:600]}{'…' if len(snippet) > 600 else ''}")

    if record.report_path:
        lines.append(f"\n📄 Звіт: <code>{record.report_path}</code>")
    return "\n".join(lines)


def send_telegram(text: str, token: str, chat_id: str) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        response = httpx.post(url, json=payload, timeout=15.0)
        if response.status_code == 200:
            return True, "ok"
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except httpx.HTTPError as exc:
        return False, f"мережева помилка: {exc}"


def deliver(
    record: RunRecord,
    settings,
    book: Book | None = None,
    *,
    always: bool = False,
) -> bool:
    """Надсилає повідомлення в Telegram (якщо налаштовано) і пише в outbox.

    always=True — ігнорує фільтр only_actionable (для збірок та помилок).
    """
    cfg = settings.notify_cfg
    text = format_record(record, book)

    only_actionable = bool(cfg.get("only_actionable", True))
    is_error = record.status == "error"
    skip = False
    if not always:
        if is_error and not cfg.get("notify_on_error", True):
            skip = True
        elif only_actionable and not (record.approved and record.action in ("BUY", "SELL")):
            skip = True

    _append_outbox(settings.outbox_path, text, skipped=skip)
    if skip:
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not cfg.get("telegram", True) or not token or not chat_id:
        return False

    ok, detail = send_telegram(text, token, chat_id)
    _append_outbox(settings.outbox_path, f"[telegram] {detail}", skipped=False)
    return ok


def deliver_summary(records: list[RunRecord], settings, book: Book | None) -> bool:
    """Ранкова/вечірня збірка: один повідомлення на весь watchlist."""
    if not records:
        return False
    approved = [r for r in records if r.approved]
    errors = [r for r in records if r.status == "error"]
    lines = [
        f"📊 <b>Аналіз завершено</b> — {len(records)} тікерів",
        f"✅ схвалено: {len(approved)} · ❌ помилок: {len(errors)}",
        "",
    ]
    for r in records:
        emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪️"}.get(r.action or "", "❔")
        tail = "✅" if r.approved else "⛔"
        lines.append(f"{emoji} <b>{r.ticker}</b> {r.action or '—'} · впевн. {r.confidence or 0:.2f} {tail}")
        if r.approved and r.quantity:
            lines.append(f"    → {r.quantity} шт. ≈ {_fmt_money(r.notional)}")
    if book is not None:
        lines += ["", f"Рахунок: {_fmt_money(book.equity)} · P/L {book.daily_pnl_pct():+.2f}%"]
    lines += ["", "Рішення за вами — ордери тут не ставляться."]
    text = "\n".join(lines)

    _append_outbox(settings.outbox_path, text, skipped=False)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not settings.notify_cfg.get("telegram", True) or not token or not chat_id:
        return False
    ok, _ = send_telegram(text, token, chat_id)
    return ok


def _append_outbox(path: Path, text: str, *, skipped: bool) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tag = "SKIP" if skipped else "SEND"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n===== [{stamp}] {tag} =====\n{text}\n")


def book_from_settings(settings) -> Book:
    return Book.from_dict(settings.portfolio())


def notify_config_status() -> dict[str, Any]:
    return {
        "telegram_token": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
        "telegram_chat": bool(os.getenv("TELEGRAM_CHAT_ID", "").strip()),
    }
