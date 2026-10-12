"""HTML-сторінки дашборда. Без зовнішніх CSS/JS — усе inline, працює офлайн."""

from __future__ import annotations

from html import escape

from .chart import levels_from_proposal
from .models import RunRecord

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0d1117; color: #e6edf3;
       font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Ubuntu, sans-serif; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1240px; margin: 0 auto; padding: 22px 18px 60px; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; justify-content: space-between;
         border-bottom: 1px solid #21262d; padding-bottom: 14px; margin-bottom: 20px; }
h1 { font-size: 19px; margin: 0; letter-spacing: .2px; }
h2 { font-size: 14px; margin: 28px 0 10px; color: #c9d1d9; text-transform: uppercase; letter-spacing: .07em; }
.sub { color: #8b949e; font-size: 12px; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 13px 15px; }
.card .k { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; }
.card .v { font-size: 20px; font-weight: 600; margin-top: 5px; font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #21262d;
        border-radius: 10px; overflow: hidden; }
th, td { padding: 9px 11px; text-align: left; border-bottom: 1px solid #21262d; vertical-align: top; }
th { background: #1c2128; color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.buy { background: #0f2f1d; color: #3fb950; border: 1px solid #238636; }
.sell { background: #3d1418; color: #f85149; border: 1px solid #8b2d2d; }
.hold { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
.ok { color: #3fb950; } .no { color: #f85149; } .warn { color: #d29922; }
button { background: #21262d; color: #e6edf3; border: 1px solid #30363d; border-radius: 7px;
         padding: 7px 13px; font-size: 13px; cursor: pointer; }
button:hover { background: #30363d; }
button.primary { background: #1f6feb; border-color: #1f6feb; }
button.primary:hover { background: #388bfd; }
button:disabled { opacity: .5; cursor: not-allowed; }
input, textarea { background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 7px;
                  padding: 7px 10px; font-size: 13px; font-family: inherit; }
textarea { width: 100%; min-height: 150px; font-family: ui-monospace, monospace; font-size: 12px; }
.row { display: flex; gap: 9px; flex-wrap: wrap; align-items: center; }
.banner { background: #2d2208; border: 1px solid #5c4a12; color: #e3b341; padding: 10px 13px;
          border-radius: 9px; margin-bottom: 12px; font-size: 13px; }
.banner.err { background: #2d1114; border-color: #5c1f22; color: #ff7b72; }
.banner.info { background: #0d2533; border-color: #1f4b63; color: #79c0ff; }
.banner.demo { background: #2b1d3a; border-color: #5a3d7a; color: #d2b3ff; }
pre { background: #0d1117; border: 1px solid #21262d; border-radius: 9px; padding: 14px;
      overflow: auto; font-size: 12px; line-height: 1.5; white-space: pre-wrap; }
details { margin-top: 6px; } summary { cursor: pointer; color: #8b949e; font-size: 12px; }
.chart { margin: 14px 0 4px; }
.footer { margin-top: 34px; color: #6e7681; font-size: 11px; border-top: 1px solid #21262d; padding-top: 14px; }
.pill { display:inline-block; background:#21262d; border:1px solid #30363d; border-radius:6px; padding:2px 7px; font-size:11px; color:#8b949e; margin-right:5px;}
"""


def _page(title: str, body: str, *, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html lang="uk"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{meta}<title>{escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">{body}
<div class="footer">Trading Assistant · пропозиції за добовим графіком · ордери тут не ставляться.
Рівні й розмір рахує код, рішення ухвалює людина. Не є фінансовою порадою.</div>
</div></body></html>"""


def action_tag(action: str | None) -> str:
    if action == "BUY":
        return '<span class="tag buy">BUY</span>'
    if action == "SELL":
        return '<span class="tag sell">SELL</span>'
    if action == "HOLD":
        return '<span class="tag hold">HOLD</span>'
    return '<span class="tag hold">—</span>'


def _money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "—"


def _price(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "—"


def _bias_tag(bias: str) -> str:
    if bias == "BUY":
        return '<span class="tag buy">BUY</span>'
    if bias in ("SELL", "SELL_WARN"):
        return '<span class="tag sell">ВИХІД</span>'
    return '<span class="tag hold">БЕЗ СЕТАПУ</span>'


_WEEKDAYS_UK = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


def _today_et_iso() -> str:
    from . import market as market_module

    return market_module.now_et().date().isoformat()


def _session_label(session: str) -> str:
    from datetime import date as _date

    try:
        day = _date.fromisoformat(session)
    except ValueError:
        return session
    return f"{day.day:02d}.{day.month:02d}.{day.year} ({_WEEKDAYS_UK[day.weekday()]})"


# ─── дашборд ──────────────────────────────────────────────────────────────────

def dashboard(proposals, runs: list[RunRecord], stats: dict, settings, state: dict, warning: str | None) -> str:
    session = proposals[0].for_session if proposals else (stats.get("next_session") or "—")
    banners = ""
    if warning:
        banners += f'<div class="banner">⚠️ {escape(warning)}</div>'
    if any(p.synthetic for p in proposals):
        banners += ('<div class="banner demo">🧪 Частина даних — СИНТЕТИКА (джерело було недоступне). '
                    'Такі рівні не можна використовувати для реальних угод.</div>')
    if state.get("running"):
        banners += f'<div class="banner info">⏳ Триває робота: {escape(state.get("current") or "…")} — сторінка оновиться</div>'
    minutes = stats.get("minutes_to_open")
    if minutes is not None and str(session) == _today_et_iso():
        if minutes > 0:
            banners += (
                f'<div class="banner info">⏱ До відкриття US-ринку {minutes} хв '
                f'(≈{minutes / 60:.1f} год) — план на цю сесію готовий нижче</div>'
            )
        else:
            banners += '<div class="banner info">🔔 Сесія вже відкрита (09:30–16:00 ET)</div>'
    if stats.get("kill_switch"):
        banners += '<div class="banner err">🛑 KILL-SWITCH: денний збиток перевищив ліміт — нові входи заблоковані</div>'

    bank = settings.portfolio()
    equity = bank.get("equity") or bank.get("cash") or 0
    with_plan = sum(1 for p in proposals if p.bias == "BUY")
    approved = sum(1 for p in proposals if p.approved)
    llm = settings.llm_cfg.get("provider", "none")
    cards = f"""
<div class="grid">
  <div class="card"><div class="k">Сесія пропозиції</div><div class="v" style="font-size:16px">{escape(_session_label(str(session)))}</div></div>
  <div class="card"><div class="k">Тікерів проаналізовано</div><div class="v">{len(proposals)}</div></div>
  <div class="card"><div class="k">З планом (BUY)</div><div class="v">{with_plan}</div></div>
  <div class="card"><div class="k">Схвалено ризик-гейтом</div><div class="v">{approved}</div></div>
  <div class="card"><div class="k">Джерело даних</div><div class="v" style="font-size:14px">{escape(str(proposals[0].data_source if proposals else "—"))}</div></div>
  <div class="card"><div class="k">LLM-коментар</div><div class="v" style="font-size:15px">{escape(str(llm))}</div></div>
  <div class="card"><div class="k">Equity / кеш</div><div class="v" style="font-size:16px">${equity:,.0f}</div></div>
  <div class="card"><div class="k">Пропозицій усього</div><div class="v">{stats.get("proposals_total", 0)}</div></div>
</div>"""

    rows = ""
    for p in proposals:
        if p.bias == "BUY":
            plan = (f"вхід <b>{_price(p.entry_price)}</b> ({'ринок' if p.entry_type == 'market' else 'ліміт'})<br>"
                    f"<span class='sub'>стоп {_price(p.stop_price)} · ціль {_price(p.target_price)} · R:R {p.rr or '—'}</span>")
            size = (f"{p.qty} шт.<br><span class='sub'>{_money(p.notional)} · "
                    f"ризик {p.risk_pct_of_equity or 0:.2f}%</span>") if p.qty else "<span class='no'>0 шт.</span>"
        else:
            plan = "<span class='sub'>—</span>"
            size = "<span class='sub'>—</span>"
        gate = '<span class="ok">✅ схвалено</span>' if p.approved else \
               (f'<span class="no">⛔</span><details><summary>{escape((p.gate_reasons or ["відхилено"])[0][:70])}</summary><div class="sub">{escape(chr(10).join(p.gate_reasons)).replace(chr(10), "<br>")}</div></details>')
        rows += f"""<tr>
<td><b>{escape(p.ticker)}</b>{' <span class="tag sell" style="font-size:10px">ДЕМО</span>' if p.synthetic else ''}
  <div class="sub">{escape(p.last_bar_date)} · {_price(p.last_close)} ({(p.change_pct or 0):+.2f}%)</div></td>
<td>{_bias_tag(p.bias)}<div class="sub">впевн. {(p.confidence or 0):.2f}</div></td>
<td>{plan}</td>
<td class="num">{p.rr if p.rr else '—'}</td>
<td>{size}</td>
<td>{gate}</td>
<td class="sub">{escape((p.note or '')[:150])}{'…' if p.note and len(p.note) > 150 else ''}</td>
<td class="mono"><a href="/proposal/{p.id}">деталі</a></td>
</tr>"""

    table = f"""<table>
<thead><tr><th>Тікер</th><th>Рішення</th><th>План</th><th>R:R</th><th>Розмір</th>
<th>Ризик-гейт</th><th>Коментар</th><th></th></tr></thead>
<tbody>{rows or '<tr><td colspan="8" class="sub">Ще немає пропозицій. Натисніть «Підготувати пропозиції».</td></tr>'}</tbody></table>"""

    controls = f"""
<div class="row" style="margin:16px 0">
  <button class="primary" onclick="proposeNow()" {'disabled' if state.get('running') else ''}>📅 Підготувати пропозиції на наступну сесію</button>
  <button onclick="location.reload()">↻ Оновити</button>
  <span class="sub">{escape(settings.schedule_summary())}{(' · наступний запуск: ' + escape(str(state.get('next_run')))) if state.get('next_run') else ''}</span>
</div>
<script>
async function proposeNow() {{
  const btn = event.target; btn.disabled = true; btn.textContent = '⏳ Обробка…';
  try {{
    const r = await fetch('/api/propose', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}});
    const j = await r.json();
    if (!j.started) alert(j.detail || 'не вдалося запустити');
    setTimeout(() => location.reload(), 3000);
  }} catch (e) {{ alert('Помилка: ' + e); }}
  finally {{ btn.disabled = false; btn.textContent = '📅 Підготувати пропозиції на наступну сесію'; }}
}}
</script>"""

    runs_rows = ""
    for r in runs[:12]:
        verdict = "KILL-SWITCH" if r.kill_switch else ("✅ дозволено" if r.approved else "⛔")
        runs_rows += f"""<tr><td><b>{escape(r.ticker)}</b><div class="sub">{escape(r.created_at[:16].replace('T',' '))}</div></td>
<td>{escape(r.analysis_date)}</td><td>{action_tag(r.action)} <span class="sub">{escape(r.rating or '')}</span></td>
<td class="num">{r.confidence if r.confidence is not None else '—'}</td><td>{verdict}</td>
<td class="mono">{f'<a href="/reports/{r.id}">звіт</a>' if r.report_path else '—'}</td></tr>"""
    runs_table = f"""<table>
<thead><tr><th>Тікер</th><th>Дата</th><th>Рішення</th><th>Впевн.</th><th>Гейт</th><th></th></tr></thead>
<tbody>{runs_rows or '<tr><td colspan="6" class="sub">Глибокий аналіз ще не запускався.</td></tr>'}</tbody></table>"""

    quick = _watchlist_block(settings)

    body = f"""<header>
  <div><h1>Trading Assistant — пропозиції на наступну сесію</h1>
  <div class="sub">Добові свічки (безкоштовні джерела) → технічний аналіз → план і розмір позиції → детермінований ризик-гейт</div></div>
  <div class="sub"><a href="/docs">API</a> · <a href="/settings">Налаштування</a> · <a href="/api/summary">JSON</a></div>
</header>
{banners}{cards}{controls}
<h2>Пропозиції на {escape(_session_label(str(session)))}</h2>
{table}
{quick}
<h2>Глибокий аналіз агентами (TradingAgents, опційно)</h2>
<div class="row" style="margin-bottom:10px">
  <button onclick="deepRun()">🧠 Запустити глибокий аналіз watchlist</button>
  <span class="sub">Потрібен встановлений TradingAgents і ключ LLM — інакше працює mock</span>
</div>
{runs_table}
<script>
async function deepRun() {{
  const r = await fetch('/api/run', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{all:true}})}});
  const j = await r.json(); alert(j.started ? 'Запущено у фоні' : (j.detail || 'не вдалося'));
  setTimeout(() => location.reload(), 3000);
}}
</script>"""
    return _page("Trading Assistant", body, refresh=8 if state.get("running") else None)


def _watchlist_block(settings) -> str:
    return """
<h2>Список тікерів</h2>
<div class="card">
  <div class="row">
    <input id="newTicker" placeholder="Напр. TSLA" maxlength="12" style="width:130px">
    <button onclick="addTicker()">+ Додати</button>
  </div>
  <div id="tickers" class="row" style="margin-top:12px"></div>
</div>
<script>
async function loadTickers() {
  const r = await fetch('/api/watchlist'); const j = await r.json();
  const box = document.getElementById('tickers'); box.innerHTML = '';
  if (!j.tickers.length) { box.innerHTML = '<span class="sub">Порожньо</span>'; return; }
  j.tickers.forEach(t => {
    const s = document.createElement('span'); s.className = 'tag hold'; s.style.fontSize = '12px';
    s.innerHTML = t + ' <a href="#" style="color:#f85149;text-decoration:none" onclick="delTicker(\\'' + t + '\\');return false">✕</a>';
    box.appendChild(s);
  });
}
async function addTicker() {
  const el = document.getElementById('newTicker'); if (!el.value.trim()) return;
  await fetch('/api/watchlist', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ticker: el.value.trim()})});
  el.value = ''; loadTickers();
}
async function delTicker(t) { await fetch('/api/watchlist/' + t, {method:'DELETE'}); loadTickers(); }
loadTickers();
</script>"""


# ─── деталі пропозиції ────────────────────────────────────────────────────────

def proposal_detail(proposal, chart_svg: str, settings, warning: str | None) -> str:
    banners = ""
    if warning:
        banners += f'<div class="banner">⚠️ {escape(warning)}</div>'
    if proposal.synthetic:
        banners += ('<div class="banner demo">🧪 Дані синтетичні (джерела були недоступні). '
                    'Це демонстрація конвеєра, а не реальний план угоди.</div>')
    for w in proposal.warnings:
        if "синтетич" in w or "ДЕМО" in w:
            continue
        banners += f'<div class="banner info">ℹ️ {escape(w)}</div>'

    tech = proposal.technical or {}
    tech_rows = "".join(
        f"<tr><td>{escape(str(key))}</td><td class='num mono'>{escape(str(value))}</td></tr>"
        for key, value in tech.items()
        if key not in ("signals", "ticker")
    )
    signals = "".join(f"<li>{escape(str(s))}</li>" for s in (tech.get("signals") or []))

    gate = "".join(f"<li>{escape(str(r))}</li>" for r in (proposal.gate_reasons or [])) or "<li>—</li>"
    reasons = "".join(f"<li>{escape(str(r))}</li>" for r in (proposal.reasons or [])) or "<li>—</li>"

    plan_card = ""
    if proposal.bias == "BUY":
        plan_card = f"""
<div class="grid">
  <div class="card"><div class="k">Вхід ({'ринок' if proposal.entry_type == 'market' else 'ліміт'})</div><div class="v">{_price(proposal.entry_price)}</div></div>
  <div class="card"><div class="k">Стоп-лосс</div><div class="v" style="color:#f85149">{_price(proposal.stop_price)}</div></div>
  <div class="card"><div class="k">Ціль</div><div class="v" style="color:#3fb950">{_price(proposal.target_price)}</div></div>
  <div class="card"><div class="k">R:R</div><div class="v">{proposal.rr if proposal.rr else '—'}</div></div>
  <div class="card"><div class="k">Кількість</div><div class="v">{proposal.qty} шт.</div></div>
  <div class="card"><div class="k">Обсяг позиції</div><div class="v">{_money(proposal.notional)}</div></div>
  <div class="card"><div class="k">Ризик на угоду</div><div class="v">{_money(proposal.risk_usd)}<div class="sub">{proposal.risk_pct_of_equity or 0:.2f}% капіталу</div></div></div>
  <div class="card"><div class="k">Впевненість</div><div class="v">{(proposal.confidence or 0):.2f}</div></div>
</div>"""
    else:
        plan_card = '<div class="banner">Сетапу немає — на наступну сесію план не будується. Причини нижче.</div>'

    order_block = ""
    if proposal.order_template:
        order_block = f"""
<h2>Текст заявки для брокера</h2>
<pre id="order">{escape(proposal.order_template)}</pre>
<div class="row"><button onclick="copyOrder()">📋 Скопіювати</button></div>
<script>
function copyOrder() {{
  navigator.clipboard.writeText(document.getElementById('order').innerText)
    .then(() => alert('Скопійовано')).catch(() => alert('Скопіюйте вручну'));
}}
</script>"""

    open_line = ""
    try:
        from .notify import _open_line as _open_line_fn

        open_line = _open_line_fn(proposal.for_session)
    except Exception:  # noqa: BLE001
        open_line = ""

    premarket_card = ""
    if proposal.premarket_price:
        gap = proposal.premarket_gap_pct or 0.0
        color = "#3fb950" if abs(gap) < 1 else ("#d29922" if abs(gap) < 2 else "#f85149")
        premarket_card = f"""
<div class="card" style="margin-top:10px">
  <div class="k">Премаркет (перевірка ґепу)</div>
  <div class="v" style="font-size:16px">${proposal.premarket_price:,.2f} <span style="color:{color};font-size:13px">{gap:+.2f}%</span></div>
  <div class="sub">{escape(str(proposal.premarket_as_of or ''))} · {escape(proposal.premarket_note or '')}</div>
</div>"""
    elif proposal.premarket_note:
        premarket_card = f'<div class="banner">🌅 {escape(proposal.premarket_note)}</div>'

    note_title = "Коментар LLM" if proposal.note_source.startswith("llm:") else "Коментар (складено з цифр, без LLM)"
    llm_error = f'<div class="banner">⚠️ LLM не відповів: {escape(str(proposal.llm_error))}</div>' if proposal.llm_error else ""

    body = f"""<header>
  <div><h1>{escape(proposal.ticker)} — план на {escape(_session_label(proposal.for_session))}</h1>
  <div class="sub">{_bias_tag(proposal.bias)} · {escape(open_line)} · остання закрита сесія {escape(proposal.last_bar_date)}:
  {_price(proposal.last_close)} ({(proposal.change_pct or 0):+.2f}%) · джерело: {escape(proposal.data_source)}</div></div>
  <div class="sub"><a href="/">← до дашборда</a> · <a href="/api/proposals/{proposal.id}">JSON</a></div>
</header>
{banners}{llm_error}
{plan_card}{premarket_card}
<div class="chart">{chart_svg}</div>
<h2>Чому такий план (правила, не LLM)</h2>
<div class="card"><ul style="margin:0;padding-left:18px">{reasons}</ul></div>
<h2>Вердикт ризик-гейта</h2>
<div class="card"><ul style="margin:0;padding-left:18px">{gate}</ul>
{'<div class="sub" style="margin-top:8px">⛔ Без схвалення гейта угоду не розглядаємо.</div>' if not proposal.approved and proposal.bias == 'BUY' else ''}</div>
{order_block}
<h2>{escape(note_title)}</h2>
<div class="card">{escape(proposal.note or '—').replace(chr(10), '<br>')}</div>
{'<div class="card" style="margin-top:10px"><b>Скасування сценарію:</b><br>' + escape(proposal.invalidation) + '</div>' if proposal.invalidation else ''}
<h2>Технічний паспорт добового графіка</h2>
<table><tbody>{tech_rows}</tbody></table>
{'<h2>Сигнали</h2><div class="card"><ul style="margin:0;padding-left:18px">' + signals + '</ul></div>' if signals else ''}
<h2>Опційно: глибокий аналіз агентами</h2>
<div class="row"><button onclick="deepOne()">🧠 Запустити TradingAgents по {escape(proposal.ticker)}</button>
<span class="sub">Потрібні встановлений пакет і ключ LLM</span></div>
<script>
async function deepOne() {{
  const r = await fetch('/api/run', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{tickers:['{escape(proposal.ticker)}'], summary:false}})}});
  const j = await r.json(); alert(j.started ? 'Запущено у фоні — звіт з\\'явиться на дашборді' : (j.detail || 'не вдалося'));
}}
</script>"""
    return _page(f"{proposal.ticker} — план на завтра", body)


def chart_for(proposal, bars) -> str:
    from .chart import candles_svg
    from .technical import series_for_chart

    series = series_for_chart(bars, last_n=90)
    title = (
        f"{proposal.ticker} · добові свічки (90 сесій) · RSI {proposal.technical.get('rsi14')} · "
        f"ATR {proposal.atr_pct}%"
    )
    return candles_svg(
        series["bars"],
        levels=levels_from_proposal(proposal),
        sma20=series["sma20"],
        sma50=series["sma50"],
        title=title,
    )


# ─── сторінка налаштувань ─────────────────────────────────────────────────────

def settings_page(settings, stats: dict, telegram_status: dict, book_json: str, warning: str | None,
                  llm_status: dict | None = None, data_sources: list[str] | None = None) -> str:
    banner = f'<div class="banner">⚠️ {escape(warning)}</div>' if warning else ""
    risk = settings.risk
    prop = settings.proposal_cfg
    llm_status = llm_status or {}
    llm_color = "ok" if llm_status.get("key_present") else "warn"
    # стан пошти (Resend): рахуємо заздалегідь — усередині f-рядка так читабельніше
    if telegram_status.get("resend_key") and telegram_status.get("mail_to"):
        mail_state = (f'<span class="ok">налаштовано</span> · '
                      f'{escape(str(telegram_status.get("mail_to")))}')
    else:
        missing = []
        if not telegram_status.get("resend_key"):
            missing.append("RESEND_API_KEY")
        if not telegram_status.get("mail_to"):
            missing.append("MAIL_TO")
        if telegram_status.get("email_enabled") is False:
            missing.append("notify.email: true у config.yaml")
        mail_state = f'<span class="warn">не налаштовано ({", ".join(missing) or "—"})</span>'
    mail_enabled_note = "" if telegram_status.get("email_enabled", True) else " (надсилання вимкнено в config.yaml)"
    body = f"""<header>
  <div><h1>Налаштування</h1><div class="sub">Ключі — у <code>.env</code>, параметри — у <code>config.yaml</code></div></div>
  <div class="sub"><a href="/">← до дашборда</a></div>
</header>
{banner}
<h2>Розклад запусків</h2>
<div class="card">
  <div class="sub">{escape(settings.schedule_summary())}</div>
  <div class="sub" style="margin-top:6px">Головне вікно — за 60 хв до відкриття (8:30 за Нью-Йорком), тобто завжди до відкриття,
  з урахуванням переходів на літній час США та України у різні дати.</div>
</div>
<h2>Джерела даних (безкоштовні, по порядку)</h2>
<div class="card">
  <div class="row">{''.join(f'<span class="pill">{escape(str(s))}</span>' for s in (data_sources or []))}
  <span class="sub">кеш {settings.data_cfg.get('cache_ttl_min')} хв · історія {settings.data_cfg.get('lookback_days')} сесій ·
  офлайн-фолбек: {escape(str(settings.data_cfg.get('offline_fallback')))}</span></div>
</div>
<h2>LLM-коментар (необов'язковий)</h2>
<div class="card">
  <div class="row"><b>{escape(str(llm_status.get('provider', 'none')))}</b>
  <span class="sub">модель: {escape(str(llm_status.get('model') or 'за замовчуванням'))}</span>
  <span class="{llm_color}">{'ключ знайдено' if llm_status.get('key_present') else 'ключ не потрібен/не знайдено'}</span></div>
  <div class="sub" style="margin-top:6px">{escape(str(llm_status.get('hint') or ''))}</div>
  <div class="sub" style="margin-top:6px">Безкоштовні варіанти: <code>groq</code>, <code>google</code>, <code>openrouter</code> (<code>:free</code>), <code>ollama</code>. Коментар не впливає на рівні та розмір.</div>
</div>
<h2>Правила пропозиції</h2>
<div class="grid">
  <div class="card"><div class="k">Ризик на угоду</div><div class="v">{prop.get('risk_per_trade_pct')}%</div></div>
  <div class="card"><div class="k">Мін. R:R</div><div class="v">{prop.get('min_rr')}</div></div>
  <div class="card"><div class="k">Стоп</div><div class="v" style="font-size:15px">{prop.get('atr_stop_mult')}×ATR</div></div>
  <div class="card"><div class="k">Макс. ATR</div><div class="v">{prop.get('max_atr_pct')}%</div></div>
  <div class="card"><div class="k">RSI для входу</div><div class="v" style="font-size:15px">{prop.get('rsi_buy_range')}</div></div>
</div>
<h2>Обмеження рахунку</h2>
<div class="grid">
  <div class="card"><div class="k">Макс. позиція</div><div class="v">{risk.get('max_position_pct')}%</div></div>
  <div class="card"><div class="k">Макс. позицій</div><div class="v">{risk.get('max_positions')}</div></div>
  <div class="card"><div class="k">Денний ліміт збитку</div><div class="v">{risk.get('daily_loss_limit_pct')}%</div></div>
  <div class="card"><div class="k">Мін. впевненість</div><div class="v">{risk.get('min_confidence')}</div></div>
  <div class="card"><div class="k">Шорт</div><div class="v" style="font-size:15px">{'дозволено' if risk.get('allow_short') else 'заборонено'}</div></div>
  <div class="card"><div class="k">Кулдаун</div><div class="v">{risk.get('cooldown_days')} дн.</div></div>
</div>
<h2>Сповіщення</h2>
<div class="card">
  <div class="row">
    <span>Telegram: {'<span class="ok">налаштовано</span>' if telegram_status['telegram_token'] and telegram_status['telegram_chat'] else '<span class="warn">не налаштовано (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)</span>'}</span>
    <button onclick="testNotify()">Надіслати тест</button>
  </div>
  <div class="row" style="margin-top:10px">
    <span>Пошта (Resend): {mail_state}{mail_enabled_note}</span>
    <button onclick="testMail()">Надіслати тестовий лист</button>
  </div>
  <div id="notifyResult" class="sub" style="margin-top:8px"></div>
  <div id="mailResult" class="sub" style="margin-top:8px"></div>
</div>
<h2>Портфель (data/book.json)</h2>
<textarea id="book">{escape(book_json)}</textarea>
<div class="row" style="margin-top:10px"><button class="primary" onclick="saveBook()">Зберегти портфель</button><span id="bookResult" class="sub"></span></div>
<h2>Журнал</h2>
<div class="card sub">Пропозицій: {stats.get('proposals_total', 0)} · схвалено: {stats.get('proposals_approved', 0)} ·
без сетапу: {stats.get('proposals_wait', 0)}<br>
Глибоких аналізів: {stats.get('runs_total', 0)} · помилок: {stats.get('runs_error', 0)}</div>
<script>
async function testMail() {{
  const box = document.getElementById('mailResult');
  box.textContent = 'надсилаю…';
  fetch('/api/mail/test', {{method: 'POST'}}).then(r => r.json()).then(d => {{
    box.textContent = (d.sent ? '✅ ' : '⛔ ') + d.detail;
  }}).catch(e => {{ box.textContent = '⛔ ' + e; }});
}}
async function testNotify() {{
  const r = await fetch('/api/notify/test', {{method:'POST'}}); const j = await r.json();
  document.getElementById('notifyResult').textContent = j.detail || (j.sent ? 'Надіслано' : 'Не надіслано');
}}
async function saveBook() {{
  const r = await fetch('/api/book', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: document.getElementById('book').value}});
  const j = await r.json();
  document.getElementById('bookResult').textContent = j.ok ? 'Збережено ✓' : ('Помилка: ' + (j.detail || ''));
}}
</script>"""
    return _page("Налаштування", body)


def run_detail(record: RunRecord, report_text: str, warning: str | None) -> str:
    banner = f'<div class="banner">⚠️ {escape(warning)}</div>' if warning else ""
    reasons = escape(record.reasons or "—").replace("\n", "<br>")
    conf = f"{record.confidence:.2f}" if record.confidence is not None else "—"
    verdict = "KILL-SWITCH" if record.kill_switch else ("✅ дозволено" if record.approved else "⛔ відхилено")
    body = f"""<header>
  <div><h1>Звіт глибокого аналізу: {escape(record.ticker)} · {escape(record.analysis_date)}</h1>
  <div class="sub">{escape(record.created_at)} · рушій {escape(record.engine)}</div></div>
  <div class="sub"><a href="/">← до дашборда</a></div>
</header>
{banner}
<div class="grid">
  <div class="card"><div class="k">Рішення</div><div class="v" style="font-size:17px">{action_tag(record.action)} {escape(record.rating or '')}</div></div>
  <div class="card"><div class="k">Впевненість</div><div class="v">{conf}</div></div>
  <div class="card"><div class="k">Ризик-гейт</div><div class="v" style="font-size:15px">{verdict}</div></div>
  <div class="card"><div class="k">Розмір</div><div class="v" style="font-size:17px">{record.quantity or 0} шт. · ${record.notional or 0:,.0f}</div></div>
</div>
<h2>Причини рішення ризик-гейта</h2>
<div class="card sub">{reasons}</div>
<h2>Звіт агента</h2>
<pre>{escape(report_text)[:60000] or 'Звіт не збережено'}</pre>"""
    return _page(f"Звіт {record.ticker}", body)
