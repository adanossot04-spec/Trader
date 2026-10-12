"""Статичний сайт з результатами — те, що публікується на GitHub Pages.

Бот лише готує пропозиції; цей скрипт перетворює журнал (data/assistant.db) і кеш
котирувань на набір звичайних HTML-файлів, які можна викласти на GitHub Pages.
Ніяких запитів до мережі: усі дані вже є в базі й у кеші. Якщо графіка немає —
сторінка рендериться без нього (це краще, ніж падати).

Запуск:
    python3 tools/make_site.py --out site
    python3 tools/make_site.py --out site --sessions 10 --title "Мій радар"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import market as market_module                       # noqa: E402
from app import views                                        # noqa: E402
from app.chart import candles_svg, levels_from_proposal      # noqa: E402
from app.db import Store                                     # noqa: E402
from app.settings import load_settings                       # noqa: E402

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


# ─── дрібні форматери ────────────────────────────────────────────────────────

def _dt(value: Any) -> str:
    if not value:
        return "—"
    text = str(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    kyiv = moment.astimezone(ZoneInfo("Europe/Kyiv"))
    return f"{kyiv:%d.%m.%Y %H:%M} Kyiv"


def _session(session: str) -> str:
    try:
        day = datetime.fromisoformat(str(session)).date()
    except ValueError:
        return str(session)
    return f"{day.day:02d}.{day.month:02d}.{day.year} ({WEEKDAYS[day.weekday()]})"


def _num(value: Any, digits: int = 2, prefix: str = "") -> str:
    if value is None:
        return "—"
    try:
        return f"{prefix}{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return escape(str(value))


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.2f}%"


def _bias(bias: str | None) -> str:
    return views._bias_tag(str(bias or ""))


def _gate(proposal) -> str:
    if proposal.bias != "BUY":
        # Для WAIT показуємо конкретну причину («імпульс негативний», «немає висхідної структури»),
        # а не загальне «гейт не застосовується» — інакше рядок нічого не пояснює.
        why = (proposal.reasons or proposal.gate_reasons or ["—"])[0]
        why = str(why).replace("немає сетапу на найближчу сесію: ", "")
        return (f'<span class="tag hold">не розглядається</span>'
                f'<div class="sub">{escape(why)[:110]}</div>')
    if proposal.approved:
        return '<span class="tag buy">✅ дозволено</span>'
    reason = (proposal.gate_reasons or ["—"])[0]
    return f'<span class="tag sell">⛔</span> <span class="sub">{escape(str(reason))[:70]}</span>'


def _live_cell(proposal) -> str:
    """Ціна на момент перевірки — показується для КОЖНОГО тікера, навіть «без сетапу»."""
    if getattr(proposal, "live_price", None) is None:
        return '<span class="sub">немає даних</span>'
    delta = ""
    if proposal.live_vs_close_pct is not None and abs(proposal.live_vs_close_pct) >= 0.005:
        tone = "#047857" if proposal.live_vs_close_pct >= 0 else "#b91c1c"
        delta = (f'<div class="sub" style="color:{tone}">'
                 f'{proposal.live_vs_close_pct:+.2f}% до закриття</div>')
    to_entry = ""
    if proposal.live_vs_entry_pct is not None and proposal.bias == "BUY":
        tone = "#6b7280"
        to_entry = (f'<div class="sub" style="color:{tone}">'
                    f'до входу {proposal.live_vs_entry_pct:+.2f}%</div>')
    label = getattr(proposal, "live_label", "") or ""
    return (f'{_num(proposal.live_price, 2, "$")}'
            f'<div class="sub">{escape(str(label))}</div>{delta}{to_entry}')


def _entry_label(proposal) -> str:
    if proposal.entry_price is None:
        return "—"
    kind = "ринок" if proposal.entry_type == "market" else "ліміт"
    return f"{_num(proposal.entry_price, 2, '$')} <span class=\"sub\">{kind}</span>"


# ─── сторінки ────────────────────────────────────────────────────────────────

def _depth_snapshot(settings, session: str | None) -> dict[str, Any]:
    """Зріз глибини, збережений прогоном (data/depth/<сесія>.json). Не перераховуємо."""
    folder = settings.data_dir / "depth"
    if not folder.exists():
        return {}
    candidates = []
    if session:
        candidates.append(folder / f"{session}.json")
    candidates += sorted(folder.glob("*.json"), reverse=True)
    for path in candidates:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _macro_state_label(state: str | None) -> tuple[str, str]:
    return {
        "risk_on": ("🟢", "сприятливий для ризику (risk-on)"),
        "risk_off": ("🔴", "обережний режим (risk-off)"),
        "neutral": ("🟡", "нейтральний"),
    }.get(str(state), ("⚪️", "немає даних"))


def _round_num(value, digits: int = 2) -> str:
    """Число з розумним округленням; «—», якщо даних немає."""
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return "—"


def _macro_section(snapshot: dict[str, Any], proposals: list) -> str:
    """Блок «режим ринку» + календар звітів + підсумок фундаменталу по всьому списку."""
    if not snapshot:
        return ""
    macro = snapshot.get("macro") or {}
    inputs = macro.get("inputs") or {}
    icon, label = _macro_state_label(macro.get("state"))
    reasons = "".join(f"<li>{escape(str(r))}</li>" for r in (macro.get("reasons") or []))
    cards = "".join([
        f'<div class="card"><div class="k">Режим ринку</div><div class="v" style="font-size:18px">{icon} {label}</div>'
        f'<div class="sub">бал {macro.get("score", "—")} · оновлено {escape(str(macro.get("as_of") or "—"))}</div></div>',
        f'<div class="card"><div class="k">VIX</div><div class="v">{_round_num(inputs.get("vix"), 1)}</div>'
        f'<div class="sub">попереднє: {_round_num(inputs.get("vix_prev"), 1)}</div></div>',
        f'<div class="card"><div class="k">10-річна ставка</div><div class="v">{_round_num(inputs.get("ten_year"), 2)}%</div>'
        f'<div class="sub">долар (DXY): {_round_num(inputs.get("dollar"), 2)}</div></div>',
        f'<div class="card"><div class="k">Ширина ринку</div><div class="v">'
        f'{snapshot.get("breadth_pct") if snapshot.get("breadth_pct") is not None else "—"}%</div>'
        f'<div class="sub">частка списку над SMA50</div></div>',
    ])

    # календар звітностей на найближчі дні
    calendar = snapshot.get("calendar") or {}
    watch = {p.ticker for p in proposals}
    rows = ""
    for day in sorted(calendar)[:7]:
        for item in calendar[day]:
            symbol = str(item.get("symbol", "")).upper()
            if symbol not in watch:
                continue
            rows += (f'<tr><td>{escape(symbol)}</td><td>{escape(day)}</td>'
                     f'<td class="sub">{escape(str(item.get("time") or "—"))}</td>'
                     f'<td class="num">{escape(str(item.get("epsForecast") or "—"))}</td></tr>')
    earnings_block = (f'<h3>Календар звітностей (наш список, 10 днів)</h3><table><thead><tr>'
                      f'<th>Тікер</th><th>Дата</th><th>Коли</th><th>Прогноз EPS</th></tr></thead>'
                      f'<tbody>{rows}</tbody></table>') if rows else ""

    # зведення по тікерах: фундаментал, сила, історія правила, подія
    matrix = ""
    tickers = snapshot.get("tickers") or {}
    for ticker, item in sorted(tickers.items(), key=lambda kv: str(kv[1].get("sector") or "")):
        earnings = item.get("earnings") or {}
        days_to = earnings.get("days_to")
        blocks = item.get("blocks") or []
        matrix += (
            f'<tr><td><b>{escape(ticker)}</b><div class="sub">{escape(str(item.get("sector_label") or "—"))}</div></td>'
            f'<td class="num">{item.get("score") if item.get("score") is not None else "—"}</td>'
            f'<td class="num">{_pct(item.get("rs_20_pct"))}</td>'
            f'<td class="num">{item.get("expectancy_r") if item.get("expectancy_r") is not None else "—"}'
            f'<div class="sub">{item.get("signals") or 0} сигн.</div></td>'
            f'<td class="num">{f"за {days_to} дн." if days_to is not None else "—"}</td>'
            f'<td class="sub">{escape(str(blocks[0])[:90]) if blocks else "—"}</td></tr>'
        )
    matrix_block = (f'<h3>Хто як виглядає за даними</h3><table><thead><tr>'
                    f'<th>Тікер</th><th>Фундам. бал</th><th>До SPY (20 с.)</th>'
                    f'<th>Історія правила</th><th>Звітність</th><th>Що завадило</th></tr></thead>'
                    f'<tbody>{matrix}</tbody></table>') if matrix else ""

    return f'''<h2>Глибина аналізу: режим ринку й фон</h2>
<div class="grid">{cards}</div>
<div class="card"><b>Що кажуть індикатори режиму:</b>
<ul style="margin:6px 0 0;padding-left:18px">{reasons or "<li>даних мало</li>"}</ul>
<div class="sub" style="margin-top:6px">Режим впливає на розмір: risk-off → ризик на угоду ×0.5,
VIX ≥ 30 → нові входи заборонені. Це правило коду, не «думка» моделі.</div></div>
{earnings_block}
{matrix_block}'''


def _depth_card(proposal) -> str:
    """Секція «Глибина» на сторінці розбору: дайджест, фундаментал, подія, історія, новини."""
    depth = getattr(proposal, "depth", None) or {}
    if not depth.get("enabled"):
        return ""
    fundamentals = depth.get("fundamentals") or {}
    revenue = fundamentals.get("revenue") or {}
    eps = fundamentals.get("eps") or {}
    earnings = fundamentals.get("earnings") or {}
    stats = depth.get("backtest") or {}
    macro = depth.get("macro") or {}
    news = depth.get("news") or []
    verdict = depth.get("verdict") or {}
    levels = depth.get("levels") or {}

    rows = [
        ("Сектор", escape(str(depth.get("sector_label") or "—"))),
        ("Емітент (SEC)", escape(str(fundamentals.get("company") or "—"))),
        ("Виручка (квартал)", f'{_num(revenue.get("last"), 0, "$")} <span class="sub">{escape(str(revenue.get("period") or ""))}</span>'),
        ("Виручка рік до року", _pct(revenue.get("yoy_pct"))),
        ("EPS рік до року", _pct(eps.get("yoy_pct"))),
        ("Чиста маржа", f'{_num(fundamentals.get("net_margin_pct"), 1)}%'),
        ("Найближчий звіт", (f'{escape(str(earnings.get("date")))} '
                            f'<span class="sub">через {earnings.get("days_to")} дн.</span>')
                            if earnings.get("days_to") is not None else "—"),
        ("Фундаментальний бал", f'{fundamentals.get("score", "—")} <span class="sub">(від −3 до +3)</span>'),
        ("Сила до SPY (20 с.)", _pct(depth.get("rs_20_pct"))),
        ("Історія правила", (f'{escape(str(stats.get("signals")))} сигналів · '
                             f'{_num(stats.get("win_rate"), 1)}% успішних · '
                             f'очікування {stats.get("expectancy_r")}R · '
                             f'макс. просідання {stats.get("max_dd_r")}R')
                            if stats.get("signals") else "мало сигналів для оцінки"),
        ("Опори / підтримки", f'{escape(", ".join(str(x) for x in (levels.get("resistances") or [])) or "—")} / '
                              f'{escape(", ".join(str(x) for x in (levels.get("supports") or [])) or "—")}'),
        ("Дивергенція RSI", escape(str(levels.get("divergence_note") or levels.get("divergence") or "немає"))),
        ("Вплив на впевненість", f'{depth.get("confidence_delta", 0):+}'),
        ("Вплив на ризик", f'ризик ×{depth.get("risk_factor", 1)}'),
    ]
    table = "".join(f'<tr><td class="sub">{key}</td><td>{value}</td></tr>' for key, value in rows)

    notes = "".join(f"<li>{escape(str(n))}</li>" for n in (verdict.get("notes") or []))
    blocks = "".join(f"<li>⛔ {escape(str(b))}</li>" for b in (verdict.get("blocks") or []))
    news_items = ""
    for item in news[:5]:
        published = str(item.get("published") or "")[:16]
        stamp = f' <span class="sub">{escape(published)}</span>' if published else ""
        news_items += f'<li>{escape(str(item.get("title")))}{stamp}</li>'
    news_block = (f'<h3>Заголовки (Yahoo RSS)</h3><div class="card">'
                  f'<ul style="margin:0;padding-left:18px">{news_items}</ul></div>') if news_items else ""

    digest_raw = str(depth.get("digest") or "")
    digest_body = escape(digest_raw.replace("**", ""))
    digest_body = digest_body.replace("\n\n", "<br><br>").replace("\n", "<br>")
    source_label = "правила коду" if str(depth.get("digest_source")) == "rules" else escape(str(depth.get("digest_source")))
    digest_html = (f'<h2>Що це означає (дайджест)</h2><div class="card">{digest_body}'
                   f'<div class="sub" style="margin-top:8px">джерело: {source_label}</div></div>')

    return f'''{digest_html}
<h2>Глибина: цифри, на яких ухвалено рішення</h2>
<table><tbody>{table}</tbody></table>
<div class="card"><b>Висновок шару глибини:</b>
<ul style="margin:6px 0 0;padding-left:18px">{blocks}{notes or ("<li>без зауважень</li>" if not blocks else "")}</ul>
<div class="sub" style="margin-top:6px">Дані: SEC EDGAR (фундаментал), Nasdaq (календар звітів),
Yahoo (VIX, ставка, ширина), Yahoo RSS (заголовки). Усе безкоштовно й без ключів.</div></div>
{news_block}'''


def _header(subtitle: str, links: str = "") -> str:
    return (f'<header><div><h1>📈 Пропозиції за годину до відкриття US-ринку</h1>'
            f'<div class="sub">{subtitle}</div></div>'
            f'<div class="sub">{links}</div></header>')


def _proposals_table(proposals: list, *, with_links: bool = True) -> str:
    if not proposals:
        return '<div class="card">Пропозицій ще немає — перший прогін о 8:30 ET (за годину до відкриття).</div>'
    rows = ""
    for p in proposals:
        chart_link = f'<a href="proposal-{p.id}.html">розбір</a>' if with_links and p.id else "—"
        rows += (
            f'<tr><td><b>{escape(p.ticker)}</b><div class="sub">закриття {_num(p.last_close, 2, "$")} '
            f'<span title="остання закрита сесія">{escape(str(p.last_bar_date)[5:])}</span></div></td>'
            f'<td class="num">{_live_cell(p)}</td>'
            f'<td>{_bias(p.bias)}</td>'
            f'<td class="num">{_entry_label(p)}</td>'
            f'<td class="num">{_num(p.stop_price, 2, "$")}</td>'
            f'<td class="num">{_num(p.target_price, 2, "$")}</td>'
            f'<td class="num">{_num(p.rr, 2)}</td>'
            f'<td class="num">{p.qty if p.qty else "—"}</td>'
            f'<td class="num">{_num(p.risk_usd, 0, "$")}</td>'
            f'<td>{_gate(p)}</td>'
            f'<td>{chart_link}</td></tr>'
        )
    return f'''<table><thead><tr>
<th>Тікер</th><th>Ціна зараз</th><th>Вердикт</th><th>Вхід</th><th>Стоп</th><th>Ціль</th><th>R:R</th>
<th>Шт.</th><th>Ризик</th><th>Гейт</th><th></th></tr></thead><tbody>{rows}</tbody></table>
<div class="sub" style="margin-top:6px">«Ціна зараз» — остання відома ціна на момент прогону
(премаркет / основна сесія / після закриття; якщо ринок ще не відкривався — закриття попередньої сесії).
Рівні входу, стопу та цілі рахуються виключно з <b>закритих</b> добових свічок — тому вхід може
стояти нижче або вище поточної ціни: це і є умова заявки, а не помилка даних.</div>'''


def _order_block(proposals: list) -> str:
    blocks = ""
    for p in proposals:
        if not p.order_template:
            continue
        mark = "✅" if p.approved else "⛔"
        blocks += (f'<div class="card"><div class="sub">{mark} {escape(p.ticker)} — '
                   f'текст для брокера{" (гейт НЕ схвалив — ставити не можна)" if not p.approved else ""}</div>'
                   f'<pre>{escape(p.order_template)}</pre></div>')
    if not blocks:
        return ""
    return f"<h2>Готові заявки (бот їх не виконує)</h2>{blocks}"


def _premarket_card(proposal) -> str:
    if proposal.premarket_price is None:
        return ""
    tone = "info"
    if proposal.bias == "BUY" and proposal.gate_reasons and any(
        "скасован" in str(r) for r in proposal.gate_reasons
    ):
        tone = "err"
    note = escape(str(proposal.premarket_note or ""))
    return (f'<div class="banner {tone}">🌅 Премаркет {_num(proposal.premarket_price, 2, "$")} '
            f'({_pct(proposal.premarket_gap_pct)}) — {note}</div>')


def proposal_page(proposal, settings, bars: list | None, *, back_link: str = "index.html") -> str:
    chart_svg = ""
    if bars:
        try:
            from app.technical import series_for_chart

            series = series_for_chart(bars, last_n=90)
            chart_svg = candles_svg(
                series["bars"],
                levels=levels_from_proposal(proposal),
                sma20=series["sma20"],
                sma50=series["sma50"],
                title=(f"{proposal.ticker} · добові свічки · RSI {proposal.technical.get('rsi14')} · "
                       f"ATR {proposal.atr_pct}%"),
            )
        except Exception as exc:                      # noqa: BLE001 — сайт важливіший за графік
            chart_svg = f'<div class="card sub">Графік недоступний: {escape(str(exc))}</div>'

    plan_rows = (
        ("Вердикт", _bias(proposal.bias)),
        ("Тип входу", "за ринком" if proposal.entry_type == "market" else "лімітна заявка"),
        ("Вхід", _num(proposal.entry_price, 2, "$")),
        ("Стоп", _num(proposal.stop_price, 2, "$")),
        ("Ціль", _num(proposal.target_price, 2, "$")),
        ("R:R", _num(proposal.rr, 2)),
        ("Кількість", f"{proposal.qty} шт." if proposal.qty else "—"),
        ("Обсяг", _num(proposal.notional, 0, "$")),
        ("Ризик", f'{_num(proposal.risk_usd, 0, "$")} ({_num(proposal.risk_pct_of_equity, 2)}% капіталу)'),
        ("ATR", f"{_num(proposal.atr_pct, 2)}%"),
        ("Ціна на момент перевірки",
         (f'<b>{_num(proposal.live_price, 2, "$")}</b> <span class="sub">{escape(str(proposal.live_label or ""))}</span>'
          + (f' <span class="sub">({proposal.live_vs_close_pct:+.2f}% до закриття)</span>'
             if proposal.live_vs_close_pct is not None else ""))
         if getattr(proposal, "live_price", None) is not None else "—"),
        ("Відхилення від входу",
         (f'{proposal.live_vs_entry_pct:+.2f}% — ціна зараз {"вища" if (proposal.live_vs_entry_pct or 0) > 0 else "нижча"} '
          f'за рівень входу')
         if getattr(proposal, "live_vs_entry_pct", None) is not None else "—"),
        ("Ціна на момент перевірки",
         (f'<b>{_num(proposal.live_price, 2, "$")}</b> <span class="sub">{escape(str(proposal.live_label or ""))}</span>'
          + (f' <span class="sub">({proposal.live_vs_close_pct:+.2f}% до закриття)</span>'
             if proposal.live_vs_close_pct is not None else ""))
         if getattr(proposal, "live_price", None) is not None else "—"),
        ("Відхилення від входу",
         (f'{proposal.live_vs_entry_pct:+.2f}% — ціна зараз {"вища" if (proposal.live_vs_entry_pct or 0) > 0 else "нижча"} '
          f'за рівень входу')
         if getattr(proposal, "live_vs_entry_pct", None) is not None else "—"),
        ("Джерело даних", escape(str(proposal.data_source or "—"))),
        ("Остання закрита сесія", escape(str(proposal.last_bar_date or "—"))),
    )
    plan_card = "".join(
        f'<tr><td class="sub">{escape(str(key))}</td><td class="num">{value}</td></tr>'
        for key, value in plan_rows
    )

    reasons = "".join(f"<li>{escape(str(r))}</li>" for r in (proposal.reasons or [])) or "<li>—</li>"
    gate = "".join(f"<li>{escape(str(r))}</li>" for r in (proposal.gate_reasons or [])) or "<li>—</li>"
    tech = proposal.technical or {}
    tech_rows = "".join(
        f'<tr><td class="sub">{escape(str(k))}</td><td class="num mono">{escape(str(v))}</td></tr>'
        for k, v in tech.items() if k not in ("signals", "ticker")
    )
    signals = "".join(f"<li>{escape(str(s))}</li>" for s in (tech.get("signals") or []))

    order_block = ""
    if proposal.order_template:
        order_block = (f'<h2>Текст заявки для брокера</h2><div class="card"><pre>'
                       f'{escape(proposal.order_template)}</pre>'
                       f'<div class="sub">Бот не виконує ордерів — це текст, який ви за потреби вводите вручну.</div></div>')

    invalidation = (f'<div class="card" style="margin-top:10px"><b>Скасування сценарію:</b><br>'
                    f'{escape(str(proposal.invalidation))}</div>') if proposal.invalidation else ""

    warnings = "".join(
        f'<div class="banner info">ℹ️ {escape(str(w))}</div>' for w in (proposal.warnings or [])
    )
    synthetic = ('<div class="banner demo">🧪 Дані синтетичні — це демонстрація конвеєра, а не план угоди.</div>'
                 if proposal.synthetic else "")
    llm_error = (f'<div class="banner info">LLM-коментар недоступний: '
                 f'{escape(str(proposal.llm_error))}</div>') if proposal.llm_error else ""

    body = f'''{_header(
        f'{escape(proposal.ticker)} · план на {_session(proposal.for_session)} '
        f'· підготовлено {_dt(proposal.created_at)}',
        f'<a href="{back_link}">← до списку</a>',
    )}
{synthetic}{warnings}{llm_error}
{_premarket_card(proposal)}
<div class="grid">
  <div class="card"><div class="k">Вхід</div><div class="v">{_num(proposal.entry_price, 2, "$")}</div></div>
  <div class="card"><div class="k">Стоп</div><div class="v">{_num(proposal.stop_price, 2, "$")}</div></div>
  <div class="card"><div class="k">Ціль</div><div class="v">{_num(proposal.target_price, 2, "$")}</div></div>
  <div class="card"><div class="k">R:R</div><div class="v">{_num(proposal.rr, 2)}</div></div>
  <div class="card"><div class="k">Розмір</div><div class="v">{proposal.qty or 0} шт.</div></div>
</div>
<div class="chart">{chart_svg}</div>
<h2>Паспорт плану</h2>
<table><tbody>{plan_card}</tbody></table>
<h2>Чому такий план (правила, не LLM)</h2>
<div class="card"><ul style="margin:0;padding-left:18px">{reasons}</ul></div>
<h2>Вердикт ризик-гейта</h2>
<div class="card"><ul style="margin:0;padding-left:18px">{gate}</ul></div>
{order_block}
<h2>Коментар</h2>
<div class="card">{escape(str(proposal.note or "—")).replace(chr(10), "<br>")}</div>
{invalidation}
{_depth_card(proposal)}
<h2>Технічний паспорт добового графіка</h2>
<table><tbody>{tech_rows}</tbody></table>
{'<h2>Сигнали</h2><div class="card"><ul style="margin:0;padding-left:18px">' + signals + '</ul></div>' if signals else ''}
'''
    return views._page(f"{proposal.ticker} — план на {_session(proposal.for_session)}", body)


def index_page(
    *,
    settings,
    proposals: list,
    depth: dict[str, Any] | None = None,
    history: list,
    stats: dict,
    runs: list,
    generated_at: datetime,
    repo_url: str | None,
    source_note: str,
) -> str:
    session = proposals[0].for_session if proposals else (stats.get("next_session") or "—")
    approved = [p for p in proposals if p.approved]
    with_plan = [p for p in proposals if p.bias == "BUY"]
    bank = settings.portfolio()
    equity = bank.get("equity") or bank.get("cash") or 0

    minutes = stats.get("minutes_to_open")
    banners = ""
    if minutes is not None and str(session) == market_module.now_et().date().isoformat():
        if minutes > 0:
            banners += (f'<div class="banner info">⏱ До відкриття US-ринку {minutes} хв '
                        f'(≈{minutes / 60:.1f} год) — план на цю сесію нижче</div>')
        else:
            banners += '<div class="banner info">🔔 Сесія вже відкрита (09:30–16:00 ET)</div>'
    if any(getattr(p, "synthetic", False) for p in proposals):
        banners += ('<div class="banner demo">🧪 Частина даних синтетична (джерело було недоступне) — '
                    'такі рівні не для реальних угод.</div>')
    if not any(p.premarket_price for p in proposals):
        banners += ('<div class="banner info">🌅 Премаркет-котирувань у цьому прогоні немає '
                    '(вихідний, свято або джерело недоступне).</div>')

    cards = "".join([
        f'<div class="card"><div class="k">Сесія</div><div class="v" style="font-size:16px">'
        f'{escape(_session(str(session)))}</div></div>',
        f'<div class="card"><div class="k">Планів</div><div class="v">{len(with_plan)}</div>'
        f'<div class="sub">з них схвалено гейтом: {len(approved)}</div></div>',
        f'<div class="card"><div class="k">Капітал</div><div class="v">${equity:,.0f}</div>'
        f'<div class="sub">ризик на угоду {settings.risk.get("risk_per_trade_pct", 0.5)}%</div></div>',
        f'<div class="card"><div class="k">Оновлено</div><div class="v" style="font-size:16px">'
        f'{generated_at.astimezone(ZoneInfo("Europe/Kyiv")):%d.%m %H:%M} Kyiv</div>'
        f'<div class="sub">{escape(source_note)}</div></div>',
    ])

    history_rows = ""
    for item in history:
        history_rows += (
            f'<tr><td>{escape(_session(str(item["session"])))}</td>'
            f'<td class="num">{item["total"]}</td>'
            f'<td class="num">{item["buy"]}</td>'
            f'<td class="num">{item["wait"]}</td>'
            f'<td class="num">{item["approved"]}</td>'
            f'<td class="sub">{escape(item.get("last_at", "—"))}</td></tr>'
        )
    history_table = (f'''<h2>Журнал прогонів</h2><table><thead><tr>
<th>Сесія</th><th>Усього</th><th>BUY</th><th>WAIT</th><th>Схвалено</th><th>Останній прогін</th>
</tr></thead><tbody>{history_rows}</tbody></table>''' if history_rows else "")

    runs_rows = "".join(
        f'<tr><td>{escape(str(r.ticker))}</td>'
        f'<td class="sub">{escape(str(r.analysis_date))}</td>'
        f'<td>{views.action_tag(r.action)}</td>'
        f'<td class="num">{r.confidence if r.confidence is not None else "—"}</td>'
        f'<td>{"✅" if r.approved else "⛔"}{" · KILL-SWITCH" if r.kill_switch else ""}</td></tr>'
        for r in runs[:10]
    )
    runs_block = (f'<h2>Глибокий аналіз агентами (опційно)</h2><table><thead><tr>'
                  f'<th>Тікер</th><th>Дата</th><th>Рішення</th><th>Впевн.</th><th>Гейт</th>'
                  f'</tr></thead><tbody>{runs_rows}</tbody></table>') if runs_rows else ""

    repo_link = f' · <a href="{escape(repo_url)}">код на GitHub</a>' if repo_url else ""
    body = f'''{_header(
        f'Сесія {_session(str(session))} · бот лише готує план, ордери не виконуються · '
        f'оновлено {generated_at.astimezone(ZoneInfo("Europe/Kyiv")):%d.%m.%Y %H:%M} Kyiv',
        f'<a href="results.json">JSON</a> · <a href="results.md">Markdown</a>{repo_link}',
    )}
{banners}
<div class="grid">{cards}</div>
<h2>Пропозиції на {escape(_session(str(session)))}</h2>
{_proposals_table(proposals)}
{_macro_section(depth or {}, proposals)}
{_order_block(approved)}
{history_table}
{runs_block}
<h2>Як це працює</h2>
<div class="card">
<ul style="margin:0;padding-left:18px">
  <li>Розклад: <b>{escape(settings.schedule_summary())}</b> — тобто за годину до відкриття NYSE (9:30 ET).</li>
  <li>Рахує все код: рівні входу/стопу/цілі, R:R, кількість акцій від ризику, вердикт ризик-гейта.</li>
  <li>LLM (якщо підключений) пише лише текст коментаря — змінити цифри він не може.</li>
  <li>Премаркет 04:00–09:30 ET: якщо ціна суперечить плану, угода позначається скасованою з причиною.</li>
  <li>Джерела — безкоштовні (Yahoo без ключа); план будується на добових свічках <b>закритих</b> сесій.</li>
</ul>
</div>
'''
    return views._page("Пропозиції за годину до відкриття US-ринку", body)


# ─── збірка ──────────────────────────────────────────────────────────────────

def _load_bars(settings, ticker: str) -> list | None:
    """Свічки для графіка — тільки з кешу (жодних запитів у мережу)."""
    try:
        data = market_module.fetch_daily(ticker, settings)
        return data.bars if data and data.bars else None
    except Exception:                                  # noqa: BLE001
        return None


def build_site(
    *,
    settings,
    out_dir: Path,
    sessions: int = 5,
    pages_for_sessions: int = 1,
    repo_url: str | None = None,
    source_note: str = "GitHub Actions",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    generated_at = generated_at or datetime.now(timezone.utc)

    latest = store.latest_proposal_session()
    proposals = store.proposals_for_session(latest) if latest else []
    results = [p for p in proposals if p.bias == "BUY"]

    # Сторінки розбору — лише для останніх pages_for_sessions сесій.
    # Сайт щодня комітиться в репозиторій, тому 20 сторінок × 10 сесій означали б
    # десятки мегабайтів історії на місяць; сторінка потрібна свіжа, а решта
    # сесій лишається видимою в таблиці журналу.
    pages = 0
    recent = max(1, pages_for_sessions)
    for session in [s["session"] for s in _sessions(store, recent)]:
        for p in store.proposals_for_session(session):
            if not p.id:
                continue
            bars = _load_bars(settings, p.ticker) if p.bias == "BUY" else None
            html = proposal_page(p, settings, bars)
            (out_dir / f"proposal-{p.id}.html").write_text(html, encoding="utf-8")
            pages += 1

    stats: dict[str, Any] = {"minutes_to_open": market_module.minutes_to_open()}
    stats["next_session"] = market_module.next_open_datetime().date().isoformat()

    depth_snapshot = _depth_snapshot(settings, latest)
    index = index_page(
        settings=settings,
        proposals=proposals,
        depth=depth_snapshot,
        history=_sessions(store, sessions),
        stats=stats,
        runs=store.recent_runs(limit=10),
        generated_at=generated_at,
        repo_url=repo_url,
        source_note=source_note,
    )
    (out_dir / "index.html").write_text(index, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    payload = {
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "session": latest,
        "schedule": settings.schedule_summary(),
        "sessions": _sessions(store, sessions),
        "proposals": [_proposal_json(p) for p in proposals],
        "runs": [
            {"ticker": r.ticker, "analysis_date": str(r.analysis_date), "action": r.action,
             "rating": r.rating, "confidence": r.confidence, "approved": r.approved,
             "kill_switch": r.kill_switch, "created_at": r.created_at}
            for r in store.recent_runs(limit=20)
        ],
        "journal": store.stats(),
        "depth": {
            "macro": (depth_snapshot.get("macro") or {}),
            "breadth_pct": depth_snapshot.get("breadth_pct"),
            "calendar": depth_snapshot.get("calendar") or {},
            "tickers": depth_snapshot.get("tickers") or {},
        } if depth_snapshot else {},
    }
    (out_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "results.md").write_text(_results_md(payload), encoding="utf-8")

    return {
        "session": latest,
        "proposals": len(proposals),
        "approved": len([p for p in proposals if p.approved]),
        "pages": pages,
        "out_dir": str(out_dir),
        "payload": payload,
    }


def _sessions(store: Store, limit: int) -> list[dict]:
    """Останні сесії з журналу, від найновішої до найстарішої."""
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT for_session AS session, COUNT(*) AS total,
                      SUM(CASE WHEN bias = 'BUY' THEN 1 ELSE 0 END) AS buy,
                      SUM(CASE WHEN bias <> 'BUY' THEN 1 ELSE 0 END) AS wait,
                      SUM(CASE WHEN approved = 1 THEN 1 ELSE 0 END) AS approved,
                      MAX(created_at) AS last_at
               FROM proposals GROUP BY for_session
               ORDER BY for_session DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["last_at"] = _dt(item.get("last_at"))
        items.append(item)
    return items


def _proposal_json(p) -> dict[str, Any]:
    return {
        "id": p.id,
        "ticker": p.ticker,
        "for_session": p.for_session,
        "bias": p.bias,
        "approved": p.approved,
        "entry_type": p.entry_type,
        "entry_price": p.entry_price,
        "stop_price": p.stop_price,
        "target_price": p.target_price,
        "rr": p.rr,
        "qty": p.qty,
        "notional": p.notional,
        "risk_usd": p.risk_usd,
        "risk_pct_of_equity": p.risk_pct_of_equity,
        "atr_pct": p.atr_pct,
        "last_close": p.last_close,
        "last_bar_date": p.last_bar_date,
        "live_price": p.live_price,
        "live_label": p.live_label,
        "live_session": p.live_session,
        "live_as_of": p.live_as_of,
        "live_source": p.live_source,
        "live_vs_close_pct": p.live_vs_close_pct,
        "live_vs_entry_pct": p.live_vs_entry_pct,
        "live_price": p.live_price,
        "live_label": p.live_label,
        "live_session": p.live_session,
        "live_as_of": p.live_as_of,
        "live_source": p.live_source,
        "live_vs_close_pct": p.live_vs_close_pct,
        "live_vs_entry_pct": p.live_vs_entry_pct,
        "premarket_price": p.premarket_price,
        "premarket_gap_pct": p.premarket_gap_pct,
        "premarket_note": p.premarket_note,
        "gate_reasons": p.gate_reasons,
        "reasons": p.reasons,
        "invalidation": p.invalidation,
        "order_template": p.order_template,
        "note": p.note,
        "note_source": p.note_source,
        "data_source": p.data_source,
        "synthetic": p.synthetic,
        "created_at": p.created_at,
        "confidence": p.confidence,
        "page": f"proposal-{p.id}.html",
        "depth": _depth_json(p),
    }


def _depth_json(p) -> dict[str, Any]:
    """Компактний зріз глибини для results.json (без важких списків)."""
    depth = getattr(p, "depth", None) or {}
    if not depth.get("enabled"):
        return {}
    fundamentals = depth.get("fundamentals") or {}
    return {
        "sector": depth.get("sector"),
        "sector_label": depth.get("sector_label"),
        "rs_20_pct": depth.get("rs_20_pct"),
        "fundamentals_score": fundamentals.get("score"),
        "revenue_yoy_pct": (fundamentals.get("revenue") or {}).get("yoy_pct"),
        "eps_yoy_pct": (fundamentals.get("eps") or {}).get("yoy_pct"),
        "net_margin_pct": fundamentals.get("net_margin_pct"),
        "earnings": fundamentals.get("earnings"),
        "filings": fundamentals.get("filings"),
        "news": [n.get("title") for n in (depth.get("news") or [])][:5],
        "backtest": {k: (depth.get("backtest") or {}).get(k)
                     for k in ("signals", "win_rate", "expectancy_r", "max_dd_r", "avg_days")},
        "macro_state": (depth.get("macro") or {}).get("state"),
        "levels": depth.get("levels"),
        "verdict": depth.get("verdict"),
        "risk_factor": depth.get("risk_factor"),
        "confidence_delta": depth.get("confidence_delta"),
        "digest": depth.get("digest"),
        "digest_source": depth.get("digest_source"),
    }


def _results_md(payload: dict) -> str:
    session = payload.get("session") or "—"
    lines = [
        f"# Пропозиції на {_session(str(session))}",
        "",
        f"Згенеровано: {payload['generated_at']} · розклад: {payload.get('schedule', '—')}",
        "",
        "| Тікер | Зараз | Вердикт | Вхід | Стоп | Ціль | R:R | Шт. | Ризик | Гейт |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    depth_payload = payload.get("depth") or {}
    macro = depth_payload.get("macro") or {}
    if macro:
        icon, label = _macro_state_label(macro.get("state"))
        inputs = macro.get("inputs") or {}
        lines += ["",
                  f"**Режим ринку:** {icon} {label} · VIX {inputs.get('vix') or '—'} · "
                  f"10Y {inputs.get('ten_year') or '—'}% · ширина ринку {depth_payload.get('breadth_pct') or '—'}%"]
        for reason in (macro.get("reasons") or [])[:4]:
            lines.append(f"- {reason}")
    for p in payload["proposals"]:
        gate = "✅" if p["approved"] else ("—" if p["bias"] != "BUY" else "⛔")
        entry = "—" if p["entry_price"] is None else f"{p['entry_price']:.2f} ({p['entry_type']})"
        stop = "—" if p["stop_price"] is None else f"{p['stop_price']:.2f}"
        target = "—" if p["target_price"] is None else f"{p['target_price']:.2f}"
        rr = "—" if p["rr"] is None else f"{p['rr']:.2f}"
        risk = "—" if p["risk_usd"] is None else f"${p['risk_usd']:.0f}"
        live = "—"
        if p.get("live_price") is not None:
            live = f"{p['live_price']:.2f}"
            if p.get("live_label"):
                live += f" ({p['live_label']})"
        lines.append(
            f"| {p['ticker']} | {live} | {p['bias']} | {entry} | {stop} | {target} | {rr} | "
            f"{p['qty'] or '—'} | {risk} | {gate} |"
        )
    premiums = [
        f"- **{p['ticker']}**: премаркет {p['premarket_price']:.2f}"
        + (f" ({p['premarket_gap_pct']:+.2f}%)" if p["premarket_gap_pct"] is not None else "")
        + f" — {p['premarket_note']}"
        for p in payload["proposals"] if p.get("premarket_price")
    ]
    if premiums:
        lines += ["", "## Премаркет і нічний ґеп", *premiums]

    deep_lines = []
    for p in payload["proposals"]:
        depth = p.get("depth") or {}
        if not depth:
            continue
        stats = depth.get("backtest") or {}
        earnings = depth.get("earnings") or {}
        digest = str(depth.get("digest") or "").split(chr(10))[0]
        head = (f"- **{p['ticker']}** ({depth.get('sector_label') or '—'}): "
                f"фундаментал {depth.get('fundamentals_score', '—')}, "
                f"до SPY {depth.get('rs_20_pct', '—')} п.п., "
                f"історія правила {stats.get('signals') or 0} сигн. "
                f"({stats.get('win_rate') or '—'}% успішних, {stats.get('expectancy_r') or '—'}R)"
                + (f", звітність за {earnings.get('days_to')} дн." if earnings.get('days_to') is not None else ""))
        if digest:
            head += f". {digest}"
        badges = []
        if depth.get("risk_factor", 1) != 1:
            badges.append(f"ризик ×{depth['risk_factor']}")
        if depth.get("confidence_delta"):
            badges.append(f"впевненість {depth['confidence_delta']:+}")
        for block in ((depth.get("verdict") or {}).get("blocks") or []):
            badges.append(f"⛔ {block}")
        if badges:
            head += " · " + " · ".join(str(b) for b in badges)
        deep_lines.append(head)
    if deep_lines:
        lines += ["", "## Глибина аналізу (безкоштовні джерела, без ключів)", *deep_lines]
    lines += [
        "",
        "> Бот лише готує пропозицію: рівні й розмір рахує код, вердикт дає детермінований ризик-гейт.",
        "> Ордери не виконуються. Не є фінансовою порадою.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Статичний сайт результатів для GitHub Pages")
    parser.add_argument("--out", default="site", help="тека для HTML (типово site/)")
    parser.add_argument("--sessions", type=int, default=5, help="скільки останніх сесій показати в журналі")
    parser.add_argument("--pages-for-sessions", type=int, default=1,
                        help="для скількох останніх сесій робити сторінки розбору (типово 1)")
    parser.add_argument("--repo-url", default=os.environ.get("REPO_URL", ""), help="посилання на код")
    parser.add_argument("--title", default="", help="заголовок у звіті CI (не обов'язково)")
    parser.add_argument("--summary", action="store_true",
                        help="дописати підсумок у $GITHUB_STEP_SUMMARY")
    args = parser.parse_args(argv)

    settings = load_settings()
    result = build_site(
        settings=settings,
        out_dir=Path(args.out),
        sessions=args.sessions,
        pages_for_sessions=args.pages_for_sessions,
        repo_url=args.repo_url or None,
    )
    print(f"сайт у {result['out_dir']}: сесія {result['session']}, "
          f"{result['proposals']} пропозицій ({result['approved']} схвалено), "
          f"{result['pages']} сторінок розбору")

    if args.summary:
        text = (f"## {args.title or 'Пропозиції на наступну сесію'}\n\n"
                + _results_md(result["payload"]))
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
            print("підсумок дописано в GITHUB_STEP_SUMMARY")
        else:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
