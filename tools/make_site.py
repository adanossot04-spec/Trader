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


def _entry_label(proposal) -> str:
    if proposal.entry_price is None:
        return "—"
    kind = "ринок" if proposal.entry_type == "market" else "ліміт"
    return f"{_num(proposal.entry_price, 2, '$')} <span class=\"sub\">{kind}</span>"


# ─── сторінки ────────────────────────────────────────────────────────────────

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
            f'<tr><td><b>{escape(p.ticker)}</b><div class="sub">закриття {_num(p.last_close, 2, "$")}</div></td>'
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
<th>Тікер</th><th>Вердикт</th><th>Вхід</th><th>Стоп</th><th>Ціль</th><th>R:R</th>
<th>Шт.</th><th>Ризик</th><th>Гейт</th><th></th></tr></thead><tbody>{rows}</tbody></table>'''


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
<h2>Технічний паспорт добового графіка</h2>
<table><tbody>{tech_rows}</tbody></table>
{'<h2>Сигнали</h2><div class="card"><ul style="margin:0;padding-left:18px">' + signals + '</ul></div>' if signals else ''}
'''
    return views._page(f"{proposal.ticker} — план на {_session(proposal.for_session)}", body)


def index_page(
    *,
    settings,
    proposals: list,
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

    # сторінки розбору — для всіх пропозицій останніх сесій
    pages = 0
    for session in [s["session"] for s in _sessions(store, sessions)]:
        for p in store.proposals_for_session(session):
            if not p.id:
                continue
            bars = _load_bars(settings, p.ticker) if p.bias == "BUY" else None
            html = proposal_page(p, settings, bars)
            (out_dir / f"proposal-{p.id}.html").write_text(html, encoding="utf-8")
            pages += 1

    stats: dict[str, Any] = {"minutes_to_open": market_module.minutes_to_open()}
    stats["next_session"] = market_module.next_open_datetime().date().isoformat()

    index = index_page(
        settings=settings,
        proposals=proposals,
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
        "page": f"proposal-{p.id}.html",
    }


def _results_md(payload: dict) -> str:
    session = payload.get("session") or "—"
    lines = [
        f"# Пропозиції на {_session(str(session))}",
        "",
        f"Згенеровано: {payload['generated_at']} · розклад: {payload.get('schedule', '—')}",
        "",
        "| Тікер | Вердикт | Вхід | Стоп | Ціль | R:R | Шт. | Ризик | Гейт |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for p in payload["proposals"]:
        gate = "✅" if p["approved"] else ("—" if p["bias"] != "BUY" else "⛔")
        entry = "—" if p["entry_price"] is None else f"{p['entry_price']:.2f} ({p['entry_type']})"
        stop = "—" if p["stop_price"] is None else f"{p['stop_price']:.2f}"
        target = "—" if p["target_price"] is None else f"{p['target_price']:.2f}"
        rr = "—" if p["rr"] is None else f"{p['rr']:.2f}"
        risk = "—" if p["risk_usd"] is None else f"${p['risk_usd']:.0f}"
        lines.append(
            f"| {p['ticker']} | {p['bias']} | {entry} | {stop} | {target} | {rr} | "
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
    lines += [
        "",
        "> Бот лише готує пропозицію: рівні й розмір рахує код, вердикт дає детермінований ризик-гейт.",
        "> Ордери не виконуються. Не є фінансовою порадою.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Статичний сайт результатів для GitHub Pages")
    parser.add_argument("--out", default="site", help="тека для HTML (типово site/)")
    parser.add_argument("--sessions", type=int, default=5, help="скільки останніх сесій показати")
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
