"""Пропозиція на наступну торгову сесію за добовим графіком.

Порядок роботи:
  1. добові свічки з безкоштовного джерела (app/market.py)
  2. детермінований технічний аналіз (app/technical.py)
  3. правила сценарію: напрямок, рівень входу, стоп, ціль, R:R
  4. ризик-гейт і розмір позиції від ризику на угоду (app/risk.py)
  5. необов'язковий текстовий коментар від LLM (app/llm.py) — він НЕ впливає на цифри

Усі числа рахує код. LLM у кращому разі пише пояснення.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date as date_cls, datetime, timezone
from typing import Any

from . import depth as depth_module
from . import llm as llm_module
from . import market as market_module
from . import technical as tech_module
from .db import Store
from .models import Signal
from .risk import Book, risk_gate, size_by_risk

log = logging.getLogger("assistant.proposal")


@dataclass
class Proposal:
    ticker: str
    for_session: str
    last_bar_date: str
    last_close: float
    change_pct: float
    bias: str = "WAIT"                  # BUY | SELL_WARN | WAIT
    entry_type: str = "none"            # limit | market | none
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    rr: float | None = None
    qty: int = 0
    notional: float | None = None
    risk_usd: float | None = None
    risk_pct_of_equity: float | None = None
    atr_pct: float | None = None
    confidence: float = 0.0
    approved: bool = False
    gate_reasons: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    invalidation: str = ""
    note: str = ""
    note_source: str = "rules"
    llm_error: str | None = None
    order_template: str = ""
    premarket_price: float | None = None
    premarket_gap_pct: float | None = None
    premarket_as_of: str | None = None
    premarket_note: str = ""
    data_source: str = ""
    synthetic: bool = False
    warnings: list[str] = field(default_factory=list)
    technical: dict[str, Any] = field(default_factory=dict)
    # Глибина: фундаментал, подія, режим ринку, сектор, історія правила, дайджест.
    # Зберігається разом із пропозицією, щоб сторінка показувала ті самі цифри,
    # на яких ухвалено рішення (а не перераховані заново).
    depth: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    id: int | None = None

    @property
    def actionable(self) -> bool:
        return self.approved and self.bias == "BUY" and self.qty > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_premarket(proposal: Proposal, settings, *, quote: dict[str, Any] | None = None) -> Proposal:
    """Оцінка нічного ґепу за премаркет-ціною. Викликається за годину до відкриття.

    Правила:
      • премаркет нижче стопу → угоду скасовуємо (вхід уже не має сенсу);
      • ґеп вниз понад max_gap_down_pct → попередження «переглянути план»;
      • ґеп вгору понад max_gap_pct → лімітна заявка не виконається, потрібне нове рішення.
    """
    cfg = settings.proposal_cfg
    if not cfg.get("premarket_check", True) or proposal.bias != "BUY":
        return proposal

    if quote is None:
        # премаркет-ціну віддає Yahoo, тож без нього в джерелах перевірка неможлива
        sources = [str(src).lower() for src in (settings.data_cfg.get("sources") or [])]
        if "yahoo" not in sources:
            return proposal
        quote = market_module.premarket_quote(proposal.ticker)
    if not quote or quote.get("session_date") != proposal.for_session:
        # премаркету на потрібну сесію ще немає (напр. прогін задовго до відкриття)
        return proposal

    price = float(quote["price"])
    proposal.premarket_price = price
    proposal.premarket_as_of = quote.get("as_of")
    if proposal.last_close:
        proposal.premarket_gap_pct = round((price - proposal.last_close) / proposal.last_close * 100, 2)
    gap = proposal.premarket_gap_pct or 0.0

    max_gap_pct = float(cfg.get("max_gap_pct", 1.0))
    max_gap_down = float(cfg.get("max_gap_down_pct", 2.0))

    if proposal.stop_price and price <= proposal.stop_price:
        proposal.approved = False
        proposal.qty = 0
        proposal.notional = None
        proposal.premarket_note = (
            f"Премаркет ${price:.2f} (ґеп {gap:+.2f}%) уже нижче стопу ${proposal.stop_price:.2f} — "
            "угоду скасовано, вхід не виконуємо."
        )
        proposal.gate_reasons.append(proposal.premarket_note)
        return proposal

    if gap <= -max_gap_down:
        proposal.approved = False
        proposal.qty = 0
        proposal.notional = None
        proposal.premarket_note = (
            f"Ґеп униз {gap:+.2f}% (премаркет ${price:.2f}) — план застарів, потрібен новий розрахунок рівнів."
        )
        proposal.gate_reasons.append(proposal.premarket_note)
        return proposal

    if gap >= max_gap_pct:
        proposal.premarket_note = (
            f"Ґеп угору {gap:+.2f}% (премаркет ${price:.2f}): лімітна заявка на ${proposal.entry_price:.2f} "
            "не виконається — або вхід за ринком на відкритті, або новий план."
        )
        proposal.gate_reasons.append(proposal.premarket_note)
    else:
        proposal.premarket_note = (
            f"Премаркет ${price:.2f} ({gap:+.2f}%) — ґеп у межах, план лишається чинним."
        )
    return proposal


# ─── правила сценарію ────────────────────────────────────────────────────────

def plan_from_technical(tech: tech_module.Technical, settings, *, has_position: bool) -> dict[str, Any]:
    """Детермінований план на наступну сесію. Повертає словник без жодного LLM."""
    cfg = settings.proposal_cfg
    atr = tech.atr14 or 0.0
    rsi = tech.rsi14 if tech.rsi14 is not None else 50.0
    rsi_buy_lo, rsi_buy_hi = (cfg.get("rsi_buy_range") or [42, 72])[:2]
    max_atr_pct = float(cfg.get("max_atr_pct", 6.0))
    min_rr = float(cfg.get("min_rr", 2.0))
    stop_mult = float(cfg.get("atr_stop_mult", 1.5))
    pullback_atr = float(cfg.get("entry_pullback_atr", 0.5))
    max_stop_pct = float(cfg.get("max_stop_pct", 8.0))

    reasons: list[str] = []
    if atr <= 0:
        return {"bias": "WAIT", "confidence": 0.0, "reasons": ["немає ATR — замало даних"]}

    if tech.atr_pct > max_atr_pct:
        return {
            "bias": "WAIT",
            "confidence": 0.0,
            "reasons": [f"ATR {tech.atr_pct:.2f}% перевищує ліміт {max_atr_pct:.1f}% — занадто волатильно для плану"],
        }

    # ── підтвердження для бичачого сценарію ──
    confirmations = 0
    if tech.trend == "up":
        confirmations += 2
        reasons.append("тренд висхідний: ціна над SMA50 і SMA20 > SMA50")
    if tech.sma200 and tech.last_close > tech.sma200:
        confirmations += 1
        reasons.append("ціна над SMA200 — довгострокова структура бичача")
    if tech.macd_hist is not None and tech.macd_hist_prev is not None and tech.macd_hist > 0:
        confirmations += 1
        rising = tech.macd_hist > tech.macd_hist_prev
        reasons.append("MACD-гістограма додатна" + (" і зростає" if rising else " (але сповільнюється)"))
    if rsi_buy_lo <= rsi <= rsi_buy_hi:
        confirmations += 1
        reasons.append(f"RSI {rsi:.1f} — у робочому діапазоні (без перекупленості)")
    if tech.vol_ratio >= 1.2:
        confirmations += 1
        reasons.append(f"обсяг вищий за середній ×{tech.vol_ratio:.2f} — рух підтверджений")
    if tech.dist_high20_pct >= -1.5:
        confirmations += 1
        reasons.append(f"ціна за {tech.dist_high20_pct:+.2f}% від максимуму 20 сесій — близько до пробою")

    bull_ok = (
        tech.trend == "up"
        and rsi_buy_lo <= rsi <= rsi_buy_hi
        and (tech.macd_hist or 0) > -abs(atr) * 0.02   # не вимагаємо строго додатного MACD, але не «мертво»
    )

    if not bull_ok:
        missing: list[str] = []
        if tech.trend != "up":
            missing.append("немає висхідної структури (ціна/SMA50/SMA20)")
        if not (rsi_buy_lo <= rsi <= rsi_buy_hi):
            missing.append(f"RSI {rsi:.1f} поза робочим діапазоном {rsi_buy_lo}-{rsi_buy_hi}")
        if (tech.macd_hist or 0) <= -abs(atr) * 0.02:
            missing.append("імпульс негативний (MACD нижче сигнальної)")
        if tech.atr_pct > max_atr_pct * 0.85:
            missing.append(f"волатильність висока (ATR {tech.atr_pct:.2f}%)")
        return {
            "bias": "WAIT",
            "confidence": 0.35,
            "reasons": ["немає сетапу на найближчу сесію: " + "; ".join(missing)],
        }

    # ── рівні ──
    # Ризик на угоду ФІКСОВАНИЙ: stop = рівно atr_stop_mult × ATR від входу, і при цьому
    # не вище за мінімум 20 сесій (щоб шум не вибивав). Якщо структура вимагає глибшого
    # стопу — тягнемо вниз рівень ВХОДУ (чекаємо відкат), а не розширюємо ризик.
    close = tech.last_close
    risk_per_share = stop_mult * atr
    max_pullback_atr = float(cfg.get("max_pullback_atr", 3.0))

    # короткострокова структура (10 сесій) важливіша за 20: для лідерів біля максимумів
    # стоп за 20-денним мінімумом був би невиправдано далеким
    structure_stop = min(tech.low10 * 0.995, close - risk_per_share)
    entry = min(close, structure_stop + risk_per_share)
    stop = entry - risk_per_share

    if entry <= 0 or stop <= 0:
        return {"bias": "WAIT", "confidence": 0.4, "reasons": ["не вдалося побудувати коректні рівні"]}

    pullback_atr = (close - entry) / atr if atr else 0.0
    if pullback_atr > max_pullback_atr:
        return {
            "bias": "WAIT",
            "confidence": 0.4,
            "reasons": [
                f"структура вимагає входу аж на {entry:.2f} (−{pullback_atr:.1f}×ATR від закриття) — "
                f"чекаємо глибшого відкату або пропускаємо"
            ],
        }

    near_close = (close - entry) <= close * 0.0015
    entry_type = "market" if near_close else "limit"
    if entry_type == "market":
        entry = close
        reasons.append("відкату немає — план передбачає вхід за ринком на відкритті")
    else:
        reasons.append(
            f"лімітна заявка на відкат: {entry:.2f} (закриття {close:.2f}, стоп під мінімумом 10 сесій {tech.low10:.2f})"
        )

    risk_pct = risk_per_share / entry * 100
    if risk_pct > max_stop_pct:
        return {
            "bias": "WAIT",
            "confidence": 0.4,
            "reasons": [f"ризик на угоду {risk_pct:.2f}% перевищує ліміт {max_stop_pct:.0f}% — занадто волатильно"],
        }

    target = max(tech.high20 * 1.005, entry + min_rr * risk_per_share)
    rr = (target - entry) / risk_per_share
    confidence = min(0.9, 0.45 + 0.07 * confirmations)

    return {
        "bias": "BUY",
        "entry_type": entry_type,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "rr": round(rr, 2),
        "risk_per_share": round(risk_per_share, 2),
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "confirmations": confirmations,
    }


def rule_note(proposal: Proposal, tech: tech_module.Technical) -> str:
    """Коментар без LLM — зібраний із фактичних цифр."""
    if proposal.bias == "WAIT":
        return (
            "Сетапу на найближчу сесію немає. " + " ".join(proposal.reasons) + " "
            f"Спостерігаємо: ключова підтримка {tech.low20:.2f}, опір {tech.high20:.2f}."
        )
    direction = "висхідний" if proposal.bias == "BUY" else "низхідний"
    return (
        f"Сценарій {direction}: тренд {tech.trend}, RSI {tech.rsi14:.1f}, ATR {tech.atr_pct:.2f}% "
        f"(≈{tech.atr14:.2f} пункту). План: вхід {proposal.entry_price:.2f} ({'за ринком' if proposal.entry_type == 'market' else 'ліміт'}), "
        f"стоп {proposal.stop_price:.2f} (ризик {proposal.risk_pct_of_equity or 0:.2f}% капіталу на {proposal.qty} шт.), "
        f"ціль {proposal.target_price:.2f} (R:R {proposal.rr}). "
        f"Опір {tech.high20:.2f}, підтримка {tech.low20:.2f}."
    )


def build_order_template(proposal: Proposal) -> str:
    if proposal.bias != "BUY" or not proposal.entry_price or not proposal.qty:
        return ""
    kind = "MARKET (на відкритті)" if proposal.entry_type == "market" else f"LIMIT {proposal.entry_price:.2f} (GTC)"
    lines = [
        f"КУПІВЛЯ {proposal.qty} × {proposal.ticker}  —  {kind}",
        f"STOP-LOSS: {proposal.stop_price:.2f}  (GTC, або перевіряйте вручну щодня)",
        f"TAKE-PROFIT: {proposal.target_price:.2f}",
    ]
    if proposal.entry_type == "limit":
        lines.append(f"Якщо на відкритті ціна вище {proposal.entry_price * 1.01:.2f} — заявку скасувати.")
    if proposal.premarket_note:
        lines.append(f"// {proposal.premarket_note}")
    return "\n".join(lines)


# ─── збірка пропозиції ────────────────────────────────────────────────────────

def build_proposal(
    settings,
    store: Store | None,
    ticker: str,
    *,
    book: Book | None = None,
    use_llm: bool = True,
    force_refresh: bool = False,
    depth_packet: dict[str, Any] | None = None,
    depth_context: dict[str, Any] | None = None,
    approved_sectors: dict[str, int] | None = None,
) -> Proposal:
    ticker = ticker.upper()
    market_data = market_module.fetch_daily(ticker, settings, force_refresh=force_refresh)
    tech = tech_module.analyze(ticker, market_data.bars)
    book = book or (store_current_book(settings) if store is None else None) or Book.from_dict(settings.portfolio())

    plan = plan_from_technical(tech, settings, has_position=book.position(ticker) is not None)
    proposal = Proposal(
        ticker=ticker,
        for_session=market_module.next_session_date(market_data.bars).isoformat(),
        last_bar_date=tech.last_date,
        last_close=tech.last_close,
        change_pct=tech.change_pct,
        bias=plan.get("bias", "WAIT"),
        entry_type=plan.get("entry_type", "none"),
        entry_price=plan.get("entry"),
        stop_price=plan.get("stop"),
        target_price=plan.get("target"),
        rr=plan.get("rr"),
        atr_pct=tech.atr_pct,
        confidence=float(plan.get("confidence", 0.0)),
        reasons=list(plan.get("reasons", [])),
        technical=tech.to_dict(),
        data_source=market_data.source,
        synthetic=market_data.synthetic,
        warnings=list(market_data.warnings),
    )

    # ── шар глибини: фундаментал, подія, режим ринку, сектор, історія правила ──
    depth_cfg = settings.depth
    depth_verdict: dict[str, Any] = {"allowed": True, "blocks": [], "notes": [], "risk_factor": 1.0}
    depth_info: dict[str, Any] = {"enabled": False}
    confidence_delta = 0.0
    if depth_cfg.get("enabled", True):
        packet = depth_packet
        if packet is None:
            context = depth_context or depth_module.build_context(settings)
            packet = depth_module.collect(
                ticker, settings, bars=market_data.bars,
                spy_bars=context.get("spy_bars"), macro=context.get("macro"),
                calendar=context.get("calendar"),
            )
        packet["ticker"] = ticker
        depth_verdict = depth_module.deep_verdict(
            bias=proposal.bias, packet=packet, settings=settings,
            approved_sectors=approved_sectors,
        )
        confidence_delta = _depth_confidence_delta(packet, depth_verdict)
        proposal.confidence = round(min(0.95, max(0.05, proposal.confidence + confidence_delta)), 3)
        depth_info = {
            "enabled": True,
            "sector": packet.get("sector"),
            "sector_label": packet.get("sector_label"),
            "rs_20_pct": packet.get("rs_20_pct"),
            "levels": {"supports": tech.supports, "resistances": tech.resistances,
                       "divergence": tech.divergence, "divergence_note": tech.divergence_note},
            "fundamentals": packet.get("fundamentals"),
            "news": packet.get("news") or [],
            "backtest": packet.get("backtest") or {},
            "macro": packet.get("macro") or {},
            "verdict": {k: depth_verdict[k] for k in ("allowed", "blocks", "notes")},
            "risk_factor": depth_verdict["risk_factor"],
            "confidence_delta": confidence_delta,
        }
    risk_factor = float(depth_verdict.get("risk_factor") or 1.0)
    depth_allowed = bool(depth_verdict.get("allowed", True))

    # ── ризик-гейт і розмір ──
    gate_action = "BUY" if proposal.bias == "BUY" else "HOLD"
    signal = Signal(
        ticker=ticker,
        analysis_date=proposal.for_session,
        action=gate_action,  # type: ignore[arg-type]
        rating=proposal.bias,
        confidence=proposal.confidence,
        rationale=" ".join(proposal.reasons),
        reference_price=proposal.entry_price or tech.last_close,
    )

    verdict = _run_gate(settings, store, signal, book, proposal.for_session)
    proposal.approved = verdict.approved and depth_allowed
    if proposal.bias == "WAIT":
        # для відсутнього сетапу показуємо лише аварійні стани гейта (kill-switch),
        # а не «сигнал HOLD — дій не потрібно»
        kills = [r for r in verdict.reasons if "KILL-SWITCH" in r]
        proposal.gate_reasons = kills or ["немає сетапу — ризик-гейт не застосовується"]
    else:
        proposal.gate_reasons = list(verdict.reasons)

    if proposal.bias == "BUY" and proposal.entry_price and proposal.stop_price:
        base_risk_pct = float(settings.proposal_cfg.get("risk_per_trade_pct", 0.5))
        risk_pct = base_risk_pct * risk_factor
        risk_qty = size_by_risk(book.equity, risk_pct, proposal.entry_price, proposal.stop_price)
        gate_qty = verdict.quantity or 0
        qty = min(risk_qty, gate_qty) if (verdict.approved and depth_allowed) else 0
        proposal.qty = max(0, qty)
        if proposal.qty:
            proposal.notional = round(proposal.qty * proposal.entry_price, 2)
            proposal.risk_usd = round(proposal.qty * (proposal.entry_price - proposal.stop_price), 2)
            proposal.risk_pct_of_equity = round(proposal.risk_usd / book.equity * 100, 2) if book.equity else None
        if verdict.approved and risk_qty < gate_qty:
            proposal.gate_reasons.append(
                f"розмір урізано ризик-бюджетом {settings.proposal_cfg.get('risk_per_trade_pct')}% "
                f"капіталу: {proposal.qty} шт. (максимум за лімітом позиції — {gate_qty} шт.)"
            )
        if not verdict.approved:
            proposal.qty = 0
            proposal.notional = None

    if depth_info.get("enabled"):
        if depth_verdict["blocks"]:
            proposal.gate_reasons = list(depth_verdict["blocks"]) + list(proposal.gate_reasons)
        elif risk_factor < 1 and proposal.bias == "BUY":
            was = float(settings.proposal_cfg.get("risk_per_trade_pct", 0.5))
            now = round(was * risk_factor, 3)
            proposal.gate_reasons = list(proposal.gate_reasons) + [
                f"режим ринку зменшив ризик на угоду: {was}% → {now}% капіталу (×{risk_factor})"
            ]
        digest = llm_module.deep_digest(packet, settings) if (
            use_llm and proposal.bias == "BUY" and depth_allowed
        ) else {"text": llm_module.rules_digest(packet), "source": "rules", "error": None}
        depth_info["digest"] = digest["text"]
        depth_info["digest_source"] = digest["source"]
        if digest.get("error"):
            depth_info["digest_error"] = digest["error"]
        proposal.depth = depth_info

    proposal.invalidation = _invalidation(tech, proposal)
    # за годину до відкриття перевіряємо нічний ґеп (якщо премаркет-дані вже є)
    if str(getattr(settings, "schedule", {}).get("anchor", "pre_open")) in ("pre_open", "both"):
        apply_premarket(proposal, settings)

    # ── коментар: LLM (необов'язково) або правила ──
    if use_llm:
        note, error = llm_module.narrate(_llm_payload(proposal, tech, book), settings)
        if note:
            proposal.note = note
            proposal.note_source = f"llm:{settings.llm_cfg.get('provider')}"
        else:
            proposal.llm_error = error
    if not proposal.note:
        proposal.note = rule_note(proposal, tech)
        proposal.note_source = "rules"

    proposal.order_template = build_order_template(proposal)

    if store is not None:
        proposal.id = store.insert_proposal(proposal)
    return proposal


def _depth_confidence_delta(packet: dict[str, Any], verdict: dict[str, Any]) -> float:
    """Наскільки глибина підсилює/послаблює впевненість у плані (±0.1 максимум).

    Правила прості й перевірні: фундаментальний бал SEC (±0.02 за бал), сила
    відносно SPY, історична якість правила, дивергенція RSI, режим ринку.
    """
    delta = 0.0
    score = (packet.get("fundamentals") or {}).get("score")
    if isinstance(score, int):
        delta += 0.02 * score
    rs = packet.get("rs_20_pct")
    if isinstance(rs, (int, float)):
        if rs > 2:
            delta += 0.03
        elif rs < -2:
            delta -= 0.03
    state = (packet.get("backtest") or {}).get("_quality")
    quality = (packet.get("quality") or {}).get("state") if packet.get("quality") else state
    if quality == "good":
        delta += 0.03
    elif quality == "poor":
        delta -= 0.05
    if (packet.get("levels") or {}).get("divergence") == "bearish" or packet.get("divergence") == "bearish":
        delta -= 0.02
    macro_state = (packet.get("macro") or {}).get("state")
    if macro_state == "risk_on":
        delta += 0.03
    elif macro_state == "risk_off":
        delta -= 0.05
    return round(max(-0.1, min(0.1, delta)), 3)


def _llm_payload(proposal: Proposal, tech: tech_module.Technical, book: Book) -> dict[str, Any]:
    return {
        "ticker": proposal.ticker,
        "сесія_пропозиції": proposal.for_session,
        "остання_сесія": proposal.last_bar_date,
        "закриття": proposal.last_close,
        "зміна_за_день_%": proposal.change_pct,
        "тренд": tech.trend,
        "sma20": tech.sma20,
        "sma50": tech.sma50,
        "sma200": tech.sma200,
        "rsi14": tech.rsi14,
        "macd_hist": tech.macd_hist,
        "atr14": tech.atr14,
        "atr_%": tech.atr_pct,
        "максимум_20_сесій": tech.high20,
        "мінімум_20_сесій": tech.low20,
        "обсяг_до_середнього": tech.vol_ratio,
        "план_коду": {
            "напрямок": proposal.bias,
            "вхід": proposal.entry_price,
            "тип_входу": proposal.entry_type,
            "стоп": proposal.stop_price,
            "ціль": proposal.target_price,
            "R:R": proposal.rr,
            "кількість_акцій": proposal.qty,
            "ризик_$": proposal.risk_usd,
            "ризик_%_капіталу": proposal.risk_pct_of_equity,
        },
        "стан_рахунку": {"equity": book.equity, "cash": book.cash, "денний_P/L_%": round(book.daily_pnl_pct(), 2)},
        "глибина": {
            "сектор": (proposal.depth or {}).get("sector_label"),
            "сила_до_SPY_пп": (proposal.depth or {}).get("rs_20_pct"),
            "фундаментал_бали_SEC": ((proposal.depth or {}).get("fundamentals") or {}).get("score"),
            "фундаментал_факти": ((proposal.depth or {}).get("fundamentals") or {}).get("notes"),
            "звітність": ((proposal.depth or {}).get("fundamentals") or {}).get("earnings"),
            "режим_ринку": ((proposal.depth or {}).get("macro") or {}).get("state"),
            "історія_правила": ((proposal.depth or {}).get("backtest") or {}).get("signals"),
            "очікування_R": ((proposal.depth or {}).get("backtest") or {}).get("expectancy_r"),
            "дивергенція": ((proposal.depth or {}).get("levels") or {}).get("divergence"),
        },
    }


def _invalidation(tech: tech_module.Technical, proposal: Proposal) -> str:
    if proposal.bias != "BUY" or not proposal.stop_price:
        return ""
    parts = [f"ґеп нижче {proposal.stop_price:.2f} на відкритті — вхід не виконуємо"]
    if tech.sma20:
        parts.append(f"закриття денної свічки нижче SMA20 ({tech.sma20:.2f}) — сценарій зламано")
    parts.append(f"закриття нижче {tech.low10:.2f} (мінімум 10 сесій) — виходимо з угоди")
    return "Скасування: " + "; ".join(parts) + "."


def _run_gate(settings, store: Store | None, signal: Signal, book: Book, session: str):
    from . import risk as risk_module

    try:
        today = date_cls.fromisoformat(session)
    except ValueError:
        today = datetime.now().date()

    approved_today = 0
    recent: list[dict[str, Any]] = []
    if store is not None:
        window = int(settings.risk.get("cooldown_days", 3)) + 1
        session = today.isoformat()

        # Прогін за годину до відкриття ПЕРЕПИСУЄ вчорашній план на ту саму сесію,
        # а не конкурує з ним: власні попередні записи по цьому тікеру й сесії
        # не враховуємо ні в денному ліміті, ні в кулдауні.
        recent = [
            r.model_dump()
            for r in store.approved_runs_since(window)
            if not (r.ticker == signal.ticker and r.analysis_date == session)
        ]
        recent += [
            {"ticker": p.ticker, "created_at": p.created_at}
            for p in store.approved_proposals_since(window)
            if not (p.ticker == signal.ticker and p.for_session == session)
        ]
        approved_today = sum(
            1
            for r in store.runs_for_day(today)
            if r.approved and not (r.ticker == signal.ticker and r.analysis_date == session)
        )
        approved_today += sum(
            1
            for p in store.proposals_for_session(session)
            if p.approved and p.ticker != signal.ticker
        )

    return risk_gate(
        signal,
        book,
        settings.risk,
        today=today,
        today_approved_count=approved_today,
        recent_orders=recent,
    )


def store_current_book(settings) -> Book:
    return Book.from_dict(settings.portfolio())
