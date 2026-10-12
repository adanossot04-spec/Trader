"""Лист із планом на пошту — через Resend API (https://resend.com).

Ключі не потрібні в коді: `RESEND_API_KEY` (і за бажанням `MAIL_TO`, `MAIL_FROM`)
беруться зі змінних середовища / секретів GitHub Actions.

Що відправляємо:
  • `build_plan_email(...)` — HTML-лист «План на найближчу сесію» + текстова версія
    (для клієнтів без HTML): режим ринку, схвалені угоди з глибиною, заблоковані,
    тікери без сетапу, готові тексти заявок, посилання на сторінку результатів;
  • `send_email(...)` — власне відправка з повторною спробою при 429/5xx.

Верстка навмисно «витривала до поштових клієнтів»: таблиці, inline-стилі, без
зовнішніх картинок і без SVG — інакше Gmail і Outlook вирізають графіку й ламають
темну тему. Графіки лишаються на сторінці результатів, у листі — цифри.
"""

from __future__ import annotations

import html as html_module
import logging
import os
import time
from html import escape
from typing import Any, Iterable, Sequence

import httpx

log = logging.getLogger("assistant.mail")

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Trading Assistant <onboarding@resend.dev>"

_INK = "#111827"
_MUTED = "#6b7280"
_LINE = "#e5e7eb"
_BG = "#f3f4f6"
_CARD = "#ffffff"
_GREEN = "#047857"
_RED = "#b91c1c"
_AMBER = "#b45309"
_HEAD = "#0f172a"


# ─── дрібні форматери ────────────────────────────────────────────────────────

_WEEKDAYS_UK = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


def session_label(session: str) -> str:
    from datetime import date as date_cls

    try:
        day = date_cls.fromisoformat(str(session))
    except ValueError:
        return str(session)
    return f"{day.day:02d}.{day.month:02d}.{day.year} ({_WEEKDAYS_UK[day.weekday()]})"


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Українські множини: 1 угода, 2–4 угоди, 5+ угод."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def trades_label(count: int) -> str:
    return f"{count} {_plural(count, 'угода', 'угоди', 'угод')}"


def _money(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"${float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _num(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:+.{digits}f}%"


def _plain_pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


_TAGS = ("strong", "b", "em", "i", "br")


def _digest_body(text: Any) -> str:
    """Дайджест без абзацу про режим ринку й без новин — у листі макро вже вгорі,
    а заголовки лишаються на сторінці результатів (щоб лист не розповзався)."""
    keep: list[str] = []
    for block in str(text or "").split("\n\n"):
        head = block.strip().lstrip("*").split(".")[0]
        if head in ("Режим ринку", "Новини"):
            continue
        keep.append(block.strip())
    return "\n\n".join(keep)


def _rich(text: Any) -> str:
    """Мінімальне форматування дайджесту: **жирний**, переноси. Усе інше — екранується."""
    raw = escape(str(text or ""))
    raw = raw.replace("**", "\x00")
    parts = raw.split("\x00")
    out = []
    for index, part in enumerate(parts):
        out.append(f"<strong>{part}</strong>" if index % 2 else part)
    return "<br>".join("".join(out).split("\n"))


def _state_emoji(state: str | None) -> str:
    return {"risk_on": "🟢", "risk_off": "🔴", "neutral": "🟡"}.get(str(state), "⚪️")


def _state_label(state: str | None) -> str:
    return {"risk_on": "сприятливий (risk-on)", "risk_off": "обережний (risk-off)",
            "neutral": "нейтральний"}.get(str(state), "немає даних")


def _deep(proposal) -> dict[str, Any]:
    return (getattr(proposal, "depth", None) or {})


def _funda(proposal) -> dict[str, Any]:
    return (_deep(proposal).get("fundamentals") or {})


# ─── HTML-лист ───────────────────────────────────────────────────────────────

def _row(label: str, value: str, *, strong: bool = False) -> str:
    weight = "600" if strong else "400"
    return (
        f'<tr>'
        f'<td style="padding:6px 10px 6px 0;color:{_MUTED};font-size:13px;white-space:nowrap">{escape(label)}</td>'
        f'<td style="padding:6px 0;color:{_INK};font-size:14px;font-weight:{weight}">{value}</td>'
        f'</tr>'
    )


def _trade_card(proposal) -> str:
    depth = _deep(proposal)
    funda = _funda(proposal)
    stats = depth.get("backtest") or {}
    earnings = funda.get("earnings") or {}
    revenue = funda.get("revenue") or {}
    eps = funda.get("eps") or {}

    kind = "за ринком" if proposal.entry_type == "market" else "лімітна заявка"
    plan_rows = [
        _row("Вхід", f'{_money(proposal.entry_price)} <span style="color:{_MUTED}">({kind})</span>', strong=True),
        _row("Стоп", _money(proposal.stop_price)),
        _row("Ціль", _money(proposal.target_price)),
        _row("R:R", _num(proposal.rr), strong=True),
        _row("Кількість", f'{proposal.qty or 0} шт.'),
        _row("Обсяг", _money(proposal.notional, 0)),
        _row("Ризик", f'{_money(proposal.risk_usd, 0)} '
                     f'<span style="color:{_MUTED}">({_num(proposal.risk_pct_of_equity, 2)}% капіталу)</span>'),
        _row("Впевненість", _num(proposal.confidence, 2)),
        _row("ATR", f'{_num(proposal.atr_pct, 2)}%'),
    ]

    depth_rows = ""
    if depth.get("enabled"):
        sector = depth.get("sector_label") or depth.get("sector") or "—"
        if revenue.get("yoy_pct") is not None or eps.get("yoy_pct") is not None:
            depth_rows += _row(
                "Бізнес (SEC)",
                f'виручка {_pct(revenue.get("yoy_pct"))} YoY'
                + (f' · EPS {_pct(eps.get("yoy_pct"))} YoY' if eps.get("yoy_pct") is not None else "")
                + (f' · маржа {_plain_pct(funda.get("net_margin_pct"))}' if funda.get("net_margin_pct") is not None else ""),
            )
        if earnings.get("days_to") is not None:
            approx = f' <span style="color:{_MUTED}">(оцінка за каденцією SEC)</span>' \
                if earnings.get("source") == "sec_estimate" else ""
            tone = _RED if earnings["days_to"] <= 5 else _INK
            depth_rows += _row("Звітність",
                               f'<span style="color:{tone}">{escape(str(earnings.get("date")))} — '
                               f'через {earnings.get("days_to")} дн.</span>{approx}')
        depth_rows += _row("Сектор", escape(str(sector)))
        if depth.get("rs_20_pct") is not None:
            depth_rows += _row("Сила до SPY", f'{_num(depth.get("rs_20_pct"), 1)} п.п. за 20 сесій')
        if stats.get("signals"):
            depth_rows += _row(
                "Історія правила",
                f'{stats.get("signals")} сигналів · {_plain_pct(stats.get("win_rate"))} успішних · '
                f'очікування {_num(stats.get("expectancy_r"))}R · просідання {_num(stats.get("max_dd_r"))}R',
            )
        digest = _digest_body(depth.get("digest"))
        if digest:
            if len(digest) > 800:
                digest = digest[:800].rstrip() + "…"
            depth_rows += _row("Що це означає", _rich(digest))

    order = ""
    if proposal.order_template:
        order = (
            f'<div style="margin-top:10px;padding:10px 12px;background:{_BG};border-radius:8px;'
            f'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:{_INK};'
            f'white-space:pre-wrap">{escape(str(proposal.order_template))}</div>'
        )

    gate = ""
    if proposal.gate_reasons:
        items = "".join(f'<li style="margin:2px 0">{escape(str(r))}</li>' for r in proposal.gate_reasons[:6])
        gate = (f'<div style="margin-top:8px;color:{_MUTED};font-size:13px">'
                f'<div style="font-weight:600">Ризик-гейт і глибина</div>'
                f'<ul style="margin:4px 0 0;padding-left:18px">{items}</ul></div>')

    ticker = escape(str(proposal.ticker))
    return f'''
  <tr><td style="padding:0 0 14px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid {_LINE};border-left:4px solid {_GREEN};border-radius:10px;background:{_CARD}">
      <tr><td style="padding:14px 16px">
        <div style="font-size:18px;font-weight:700;color:{_INK}">
          ✅ {ticker}
          <span style="font-size:13px;font-weight:500;color:{_MUTED}">
            · {escape(str(depth.get("sector_label") or ""))}</span>
        </div>
        <div style="color:{_MUTED};font-size:13px;margin:2px 0 8px">
          останнє закриття {_money(proposal.last_close)} ({_pct(proposal.change_pct)}) за {escape(str(proposal.last_bar_date))}
        </div>
        <table role="presentation" cellpadding="0" cellspacing="0">{''.join(plan_rows)}</table>
        {f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:8px;border-top:1px solid {_LINE};padding-top:6px">{depth_rows}</table>' if depth_rows else ''}
        {gate}
        {order}
      </td></tr>
    </table>
  </td></tr>'''


def _blocked_table(blocked: Sequence[Any]) -> str:
    rows = "".join(
        f'<tr>'
        f'<td style="padding:7px 10px 7px 0;font-weight:600;color:{_INK};white-space:nowrap">{escape(str(p.ticker))}</td>'
        f'<td style="padding:7px 0;color:{_MUTED};font-size:13px">'
        f'{escape(str((p.gate_reasons or p.reasons or ["—"])[0]))}</td>'
        f'</tr>'
        for p in blocked
    )
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-top:1px solid {_LINE}">{rows}</table>')


def _wait_table(waiting: Sequence[Any]) -> str:
    rows = "".join(
        f'<tr>'
        f'<td style="padding:6px 10px 6px 0;font-weight:600;color:{_INK};white-space:nowrap">{escape(str(p.ticker))}</td>'
        f'<td style="padding:6px 0;color:{_MUTED};font-size:13px">'
        f'{escape(str((p.reasons or ["без сетапу"])[0]).replace("немає сетапу на найближчу сесію: ", ""))[:120]}</td>'
        f'</tr>'
        for p in waiting
    )
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _macro_strip(depth_snapshot: dict[str, Any] | None, proposals: Sequence[Any]) -> str:
    macro = (depth_snapshot or {}).get("macro") or {}
    inputs = macro.get("inputs") or {}
    if not macro and not (depth_snapshot or {}).get("breadth_pct"):
        return ""
    cells = [
        ("Режим ринку", f'{_state_emoji(macro.get("state"))} {_state_label(macro.get("state"))}'),
        ("VIX", _num(inputs.get("vix"), 1)),
        ("10-річна ставка", _num(inputs.get("ten_year"), 2, "%")),
        ("Ширина ринку", _plain_pct((depth_snapshot or {}).get("breadth_pct"), 0)
         if (depth_snapshot or {}).get("breadth_pct") is not None else "—"),
    ]
    width = 25
    html = "".join(
        f'<td width="{width}%" style="padding:10px 8px;text-align:center;'
        f'border-right:1px solid {_LINE}">'
        f'<div style="color:{_MUTED};font-size:12px">{escape(label)}</div>'
        f'<div style="color:{_INK};font-size:15px;font-weight:600;margin-top:2px">{value}</div></td>'
        for label, value in cells
    )
    reasons = "".join(f'<li style="margin:2px 0">{escape(str(r))}</li>' for r in (macro.get("reasons") or [])[:4])
    reasons_html = (f'<div style="padding:10px 16px 14px;color:{_MUTED};font-size:13px">'
                    f'<ul style="margin:0;padding-left:18px">{reasons}</ul></div>') if reasons else ""
    return (f'<tr><td style="padding:0 0 16px"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="border:1px solid {_LINE};border-radius:10px;background:{_CARD}">'
            f'<tr>{html}</tr></table>{reasons_html}</td></tr>')


def _section(title: str, subtitle: str = "") -> str:
    sub = (f'<div style="color:{_MUTED};font-size:13px;margin-top:2px">{escape(subtitle)}</div>'
           if subtitle else "")
    return (f'<tr><td style="padding:20px 0 8px"><div style="font-size:16px;font-weight:700;color:{_INK}">'
            f'{escape(title)}</div>{sub}</td></tr>')


def build_plan_email(
    proposals: Sequence[Any],
    settings,
    session: str,
    *,
    depth_snapshot: dict[str, Any] | None = None,
    results_url: str | None = None,
) -> tuple[str, str, str]:
    """Повертає (тема, HTML, текст) для листа з планом на сесію."""
    approved = [p for p in proposals if p.actionable]
    blocked = [p for p in proposals if p.bias == "BUY" and not p.approved]
    waiting = [p for p in proposals if p.bias == "WAIT"]
    label = session_label(session)

    tiny = ", ".join(str(p.ticker) for p in approved)
    subject = f"План на {label} — {trades_label(len(approved))}" + (f": {tiny}" if approved else "")
    if not approved:
        subject = f"План на {label} — входів немає ({len(waiting)} без сетапу)"

    # ── HTML ──
    generated = ""
    try:
        from . import market as market_module

        now_et = market_module.now_et()
        minutes = market_module.minutes_to_open(now_et)
        if minutes and minutes > 0:
            generated = f'⏱ до відкриття US-ринку ≈ {minutes} хв'
        else:
            generated = f'оновлено {now_et:%d.%m %H:%M} ET'
    except Exception:                                            # noqa: BLE001
        generated = ""

    header = f'''
  <tr><td style="background:{_HEAD};padding:20px 20px 16px">
    <div style="color:#ffffff;font-size:20px;font-weight:700">📈 План на {escape(label)}</div>
    <div style="color:#94a3b8;font-size:13px;margin-top:4px">
      {escape(f'{trades_label(len(approved))} із {len(proposals)} тікерів')}{f' · {escape(generated)}' if generated else ''}
    </div>
  </td></tr>'''

    parts = [header, _macro_strip(depth_snapshot, proposals)]
    if approved:
        parts.append(_section("Готові плани", "Усі рівні пораховані кодом · бот ордерів не виставляє"))
        parts.append("".join(_trade_card(p) for p in approved))
    else:
        parts.append(_section("Входів немає", "Жоден тікер не пройшов правила сьогодні — це теж рішення"))

    if blocked:
        parts.append(_section("Заблоковано фільтрами", "Сетап був, але глибина або ризик-гейт сказали «ні»"))
        parts.append(f'<tr><td style="padding:0 0 8px"><div style="border:1px solid {_LINE};'
                     f'border-left:4px solid {_RED};border-radius:10px;background:{_CARD};padding:6px 14px">'
                     f'{_blocked_table(blocked)}</div></td></tr>')

    if waiting:
        parts.append(_section("Без сетапу", "Чому саме — у колонці причин"))
        parts.append(f'<tr><td style="padding:0 0 8px"><div style="border:1px solid {_LINE};'
                     f'border-radius:10px;background:{_CARD};padding:6px 14px">{_wait_table(waiting)}</div></td></tr>')

    link_line = ""
    if results_url:
        link_line = (f'<a href="{escape(results_url)}" style="color:#2563eb;font-size:14px">'
                     f'Повна сторінка результатів з графіками →</a>')
    footer = f'''
  <tr><td style="background:{_BG};padding:16px 20px;border-top:1px solid {_LINE}">
    {link_line}
    <div style="color:{_MUTED};font-size:12px;margin-top:8px">
      Бот лише готує план: рівні, розмір і вердикт рахує код, LLM не має доступу до цифр.
      Ордери не виконуються. Не є фінансовою порадою.
    </div>
    <div style="color:{_MUTED};font-size:12px;margin-top:6px">
      Лист надіслано через Resend · керування: config.yaml → notify, ключі: RESEND_API_KEY / MAIL_TO
    </div>
  </td></tr>'''

    html = f'''<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<title>{escape(subject)}</title></head>
<body style="margin:0;padding:0;background:{_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:{_INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};padding:18px 10px">
  <tr><td align="center">
    <table role="presentation" width="680" cellpadding="0" cellspacing="0"
           style="width:680px;max-width:100%;background:{_CARD};border:1px solid {_LINE};border-radius:14px;overflow:hidden">
      {''.join(parts)}
      {footer}
    </table>
  </td></tr>
</table>
</body></html>'''

    # ── текстова версія ──
    text_lines = [f"ПЛАН НА {label}", f"{trades_label(len(approved))} із {len(proposals)} тікерів"]
    if generated:
        text_lines.append(generated)
    macro = (depth_snapshot or {}).get("macro") or {}
    if macro:
        inputs = macro.get("inputs") or {}
        text_lines += [
            "",
            f"РЕЖИМ РИНКУ: {_state_label(macro.get('state'))} · VIX {_num(inputs.get('vix'), 1)} · "
            f"10Y {_num(inputs.get('ten_year'), 2, '%')} · ширина "
            f"{_plain_pct((depth_snapshot or {}).get('breadth_pct'), 0)}",
        ]
    if approved:
        text_lines += ["", "── ГОТОВІ ПЛАНИ " + "─" * 40]
        for p in approved:
            depth = _deep(p)
            funda = _funda(p)
            stats = depth.get("backtest") or {}
            earnings = funda.get("earnings") or {}
            text_lines += [
                "",
                f"{p.ticker} — {depth.get('sector_label') or '—'}",
                f"  вхід {_money(p.entry_price)} · стоп {_money(p.stop_price)} · ціль {_money(p.target_price)} "
                f"· R:R {_num(p.rr)} · {p.qty or 0} шт. · ризик {_money(p.risk_usd, 0)}",
            ]
            if (funda.get("revenue") or {}).get("yoy_pct") is not None:
                text_lines.append(
                    f"  бізнес: виручка {_pct((funda.get('revenue') or {}).get('yoy_pct'))} YoY · "
                    f"EPS {_pct((funda.get('eps') or {}).get('yoy_pct'))} YoY · "
                    f"маржа {_plain_pct(funda.get('net_margin_pct'))}"
                )
            if earnings.get("days_to") is not None:
                text_lines.append(f"  звітність: {earnings.get('date')} (за {earnings.get('days_to')} дн.)")
            if stats.get("signals"):
                text_lines.append(
                    f"  історія правила: {stats.get('signals')} сигналів · {_plain_pct(stats.get('win_rate'))} "
                    f"успішних · очікування {_num(stats.get('expectancy_r'))}R"
                )
            digest = _digest_body(depth.get("digest")).replace("**", "")
            if digest:
                if len(digest) > 800:
                    digest = digest[:800].rstrip() + "…"
                text_lines += ["  " + line for line in digest.split("\n") if line.strip()]
            if p.order_template:
                text_lines += ["", "  ЗАЯВКА:", *["  " + ln for ln in str(p.order_template).split("\n")]]
    if blocked:
        text_lines += ["", "── ЗАБЛОКОВАНО " + "─" * 42]
        for p in blocked:
            text_lines.append(f"  ⛔ {p.ticker}: {(p.gate_reasons or p.reasons or ['—'])[0]}")
    if waiting:
        text_lines += ["", "── БЕЗ СЕТАПУ " + "─" * 43]
        for p in waiting:
            why = str((p.reasons or ["без сетапу"])[0]).replace("немає сетапу на найближчу сесію: ", "")
            text_lines.append(f"  {p.ticker}: {why[:110]}")
    if results_url:
        text_lines += ["", f"Сторінка результатів: {results_url}"]
    text_lines += ["", "Бот лише готує план: рівні й розмір рахує код. Ордери не виконуються.", "Не є фінансовою порадою."]
    return subject, html, "\n".join(text_lines)


# ─── відправка ───────────────────────────────────────────────────────────────

def recipients(settings) -> list[str]:
    raw = (settings.env_or("MAIL_TO", "") or settings.notify_cfg.get("email_to", "") or "").strip()
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def sender(settings) -> str:
    return (settings.env_or("MAIL_FROM", "") or settings.notify_cfg.get("email_from", "") or DEFAULT_FROM).strip()


def send_email(subject: str, html: str, text: str, settings, *, to: Iterable[str] | None = None) -> tuple[bool, str]:
    """Відправка через Resend. Збій не піднімається вище — пишеться в outbox і лог."""
    api_key = (settings.env_or("RESEND_API_KEY", "") or "").strip()
    if not api_key:
        return False, "RESEND_API_KEY не задано"
    to_list = [str(x).strip() for x in (to if to is not None else recipients(settings)) if str(x).strip()]
    if not to_list:
        return False, "не задано отримувача (MAIL_TO або notify.email_to у config.yaml)"

    payload = {"from": sender(settings), "to": to_list, "subject": subject, "html": html, "text": text}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_error = ""
    for attempt in range(1, 4):
        try:
            response = httpx.post(RESEND_URL, json=payload, headers=headers, timeout=20.0)
        except httpx.HTTPError as exc:
            last_error = f"мережева помилка: {type(exc).__name__}: {exc}"
        else:
            if response.status_code in (200, 201, 202):
                try:
                    mail_id = response.json().get("id")
                except ValueError:
                    mail_id = None
                return True, f"ok ({mail_id or 'без id'}) — {', '.join(to_list)}"
            body = (response.text or "")[:300]
            try:                                   # Resend відповідає {"message": "...", "name": "..."}
                parsed = response.json()
                if isinstance(parsed, dict) and parsed.get("message"):
                    body = str(parsed["message"])[:200]
            except ValueError:
                pass
            last_error = f"HTTP {response.status_code}: {body}"
            if response.status_code in (400, 401, 403, 422):       # ключ/домен/адреса — повторювати немає сенсу
                break
        if attempt < 3:
            time.sleep(1.5 * attempt)                              # 429 або 5xx — коротка пауза й ще раз
    return False, last_error


def send_plan_email(proposals: Sequence[Any], settings, session: str, *,
                    depth_snapshot: dict[str, Any] | None = None,
                    results_url: str | None = None) -> tuple[bool, str]:
    """Лист із планом. Вмикається `notify.email: true`."""
    cfg = settings.notify_cfg
    if not cfg.get("email", False):
        return False, "пошту вимкнено (notify.email: false)"
    subject, html, text = build_plan_email(
        proposals, settings, session, depth_snapshot=depth_snapshot,
        results_url=results_url or results_hint(settings),
    )
    return send_email(subject, html, text, settings)


def results_hint(settings) -> str:
    """Посилання на сторінку результатів: із змінної RESULTS_URL або CDN-шаблон."""
    explicit = (settings.env_or("RESULTS_URL", "") or "").strip()
    if explicit:
        return explicit
    repo = (settings.env_or("GITHUB_REPOSITORY", "") or "").strip()
    if repo:
        return f"https://cdn.jsdelivr.net/gh/{repo}@main/results/site/index.html"
    return ""


def mail_config_status(settings) -> dict[str, Any]:
    """Стан пошти для /settings у дашборді та для підсумку запуску."""
    key = bool((settings.env_or("RESEND_API_KEY", "") or "").strip())
    to = recipients(settings)
    return {
        "email_enabled": bool(settings.notify_cfg.get("email", False)),
        "resend_key": key,
        "mail_to": ", ".join(to),
        "mail_from": sender(settings),
        "ready": key and bool(to) and bool(settings.notify_cfg.get("email", False)),
    }


def test_email(settings, *, to: Iterable[str] | None = None) -> tuple[bool, str]:
    """Перевірка каналу: короткий лист із тим самим рендером (без даних плану)."""
    from datetime import datetime, timezone

    subject = f"✅ Trading Assistant: тест пошти ({datetime.now(timezone.utc):%d.%m %H:%M} UTC)"
    html = f'''<!doctype html><html lang="uk"><head><meta charset="utf-8"><title>{escape(subject)}</title></head>
<body style="margin:0;background:{_BG};font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:{_INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:18px 10px"><tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0"
       style="width:560px;max-width:100%;background:{_CARD};border:1px solid {_LINE};border-radius:14px;overflow:hidden">
  <tr><td style="background:{_HEAD};padding:18px 20px;color:#fff;font-size:18px;font-weight:700">
    ✅ Канал пошти працює</td></tr>
  <tr><td style="padding:16px 20px;font-size:14px">
    Це тестовий лист від бота «Пропозиції за годину до відкриття US-ринку».<br><br>
    Канал: <strong>Resend API</strong> · відправник: <code>{escape(sender(settings))}</code><br>
    Отримувач: <strong>{escape(", ".join(to or recipients(settings)) or "—")}</strong><br><br>
    Далі сюди приходитиме план на найближчу сесію: режим ринку, схвалені угоди з рівнями
    та фундаменталом, заблоковані фільтрами тікери й готові тексти заявок.
    <div style="color:{_MUTED};font-size:12px;margin-top:12px">
      Нема листа — перевірте теку «Спам» і те, що домен відправника підтверджено в Resend.</div>
  </td></tr>
</table></td></tr></table></body></html>'''
    text = (f"Канал пошти працює.\nВідправник: {sender(settings)}\n"
            f"Отримувач: {', '.join(to or recipients(settings)) or '—'}\n\n"
            "Далі сюди приходитиме план на найближчу сесію.")
    return send_email(subject, html, text, settings, to=to)
