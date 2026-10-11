#!/usr/bin/env python3
"""Точка входу: python run.py

  python run.py                 # підняти дашборд + планувальник
  python run.py --propose       # пропозиції на наступну сесію за добовим графіком (для cron)
  python run.py --run-now       # глибокий аналіз агентами TradingAgents (потрібен ключ LLM)
  python run.py --tickers NVDA,AAPL --date 2026-10-10
  python run.py --settle        # оцінити минулі рішення по alpha
  python run.py --no-schedule   # дашборд без планувальника
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from app import pipeline
from app.db import Store
from app.settings import BASE_DIR, load_settings


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    load_dotenv(BASE_DIR / ".env", override=False)
    parser = argparse.ArgumentParser(description="Помічник для торгівлі US-акціями на базі TradingAgents")
    parser.add_argument("--propose", action="store_true", help="пропозиції на наступну сесію і вихід")
    parser.add_argument("--run-now", action="store_true", help="глибокий аналіз агентами TradingAgents і вихід")
    parser.add_argument("--force", action="store_true", help="ігнорувати кеш свічок")
    parser.add_argument("--tickers", default="", help="список через кому (за замовчуванням — watchlist)")
    parser.add_argument("--date", default=None, help="дата аналізу YYYY-MM-DD")
    parser.add_argument("--settle", action="store_true", help="тільки оцінити минулі рішення")
    parser.add_argument("--no-schedule", action="store_true", help="не запускати планувальник")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    store = Store(settings.db_path)
    store.seed_watchlist(settings.watchlist)

    engine, warning = pipeline.get_engine(settings)
    print(f"→ рушій: {getattr(engine, 'name', 'unknown')}")
    if warning:
        print(f"⚠️  {warning}")

    if args.settle:
        settled = pipeline.settle_pending(settings, store)
        print(f"→ закрито рішень: {len(settled)}")
        return 0

    if args.propose:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
        proposals = pipeline.build_proposals(settings, store, tickers, force_refresh=args.force)
        session = proposals[0].for_session if proposals else "—"
        print(f"→ пропозиції на {session}: {len(proposals)}")
        for p in proposals:
            verdict = "✅" if p.approved else ("⛔" if p.bias == "BUY" else "⚪️")
            plan = (f"вхід {p.entry_price} → стоп {p.stop_price} → ціль {p.target_price} (R:R {p.rr})"
                    if p.bias == "BUY" else (p.reasons[0] if p.reasons else "—"))
            print(f"   {p.ticker:6} {p.bias:5} {verdict} {p.qty or 0:>3} шт. · {plan[:90]}")
        return 0

    if args.run_now:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
        records = pipeline.run_watchlist(settings, store, tickers)
        print(f"→ готово: {len(records)} записів")
        for r in records:
            verdict = "KILL-SWITCH" if r.kill_switch else ("✅" if r.approved else "⛔")
            print(f"   {r.ticker:6} {str(r.action):5} {verdict} {r.quantity or 0} шт. "
                  f"({r.status}{' — ' + (r.error or '')[:80] if r.status == 'error' else ''})")
        return 0

    host = args.host or os.getenv("ASSISTANT_HOST", "0.0.0.0")
    port = args.port or int(os.getenv("ASSISTANT_PORT", "8000"))

    if args.no_schedule:
        settings.config["schedule"]["enabled"] = False
        os.environ["ASSISTANT_NO_SCHEDULE"] = "1"  # сервіс читає конфіг заново у lifespan

    import uvicorn

    print(f"→ дашборд: http://localhost:{port}  (у превʼю — через проксі порту {port})")
    uvicorn.run("app.web:app", host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
