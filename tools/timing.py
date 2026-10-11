"""Замір: з чого складається час роботи бота (і чому він такий короткий).

Запуск:
    python3 tools/timing.py            # усі тікери зі списку
    python3 tools/timing.py 5          # перші п'ять

Вимірюються чотири фази:
  1) котирування — мережеві запити до Yahoo (2 роки добових свічок на тікер);
  2) премаркет — запит хвилинних барів 04:00–09:30 ET на тікер;
  3) рахунок — індикатори (SMA/EMA/RSI/MACD/ATR), правила сценарію, ризик-гейт;
  4) сторінка — збірка HTML-звіту.

Сенс вправ: уся «аналітика» тут — це арифметика над ≈ 420 свічками на тікер,
тому рахунок займає мілісекунди. Час з'їдає мережа. Саме тому 20 тікерів
укладаються в десятки секунд, тоді як LLM-аналіз (TradingAgents тощо) —
це хвилини на один тікер.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import market as market_module          # noqa: E402
from app import pipeline                         # noqa: E402
from app.db import Store                         # noqa: E402
from app.settings import load_settings           # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from make_site import build_site                 # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    settings = load_settings()
    tickers = settings.watchlist
    if argv and argv[0].isdigit():
        tickers = tickers[: int(argv[0])]

    requests = 0
    timings: dict[str, float] = {}

    started = time.perf_counter()
    for ticker in tickers:
        step = time.perf_counter()
        try:
            market_module.fetch_daily(ticker, settings, force_refresh=True)
            requests += 1
        except Exception as exc:                              # noqa: BLE001
            print(f"  {ticker}: не вдалося ({exc})")
        timings["котирування (мережа)"] = timings.get("котирування (мережа)", 0.0) + (
            time.perf_counter() - step
        )

    for ticker in tickers:
        step = time.perf_counter()
        market_module.premarket_quote(ticker)
        requests += 1
        timings["премаркет (мережа)"] = timings.get("премаркет (мережа)", 0.0) + (
            time.perf_counter() - step
        )

    store = Store(settings.db_path)
    proposals = []
    for ticker in tickers:
        step = time.perf_counter()
        proposals.append(
            pipeline.propose_one(settings, store, ticker, notify=False, use_llm=False,
                                 force_refresh=False)         # дані вже в кеші → чистий рахунок
        )
        timings["індикатори + правила + гейт"] = timings.get("індикатори + правила + гейт", 0.0) + (
            time.perf_counter() - step
        )

    step = time.perf_counter()
    build_site(settings=settings, out_dir=Path("site"), sessions=10, pages_for_sessions=1)
    timings["збірка сторінки результатів"] = time.perf_counter() - step

    total = time.perf_counter() - started
    bars = sum(1 for _ in [0])  # заповнюється нижче
    try:
        bars = len(market_module.fetch_daily(tickers[0], settings).bars)
    except Exception:                                        # noqa: BLE001
        bars = 0

    print(f"\nТікерів: {len(tickers)} · HTTP-запитів: {requests} · свічок на тікер: {bars}")
    print(f"{'фаза':40} {'секунд':>9} {'частка':>8}")
    print("-" * 60)
    for name, seconds in sorted(timings.items(), key=lambda kv: -kv[1]):
        print(f"{name:40} {seconds:9.2f} {100 * seconds / total:7.1f}%")
    print("-" * 60)
    print(f"{'РАЗОМ':40} {total:9.2f} {100:7.1f}%")
    print(f"\nЗ них рахунок (без мережі та збірки HTML): "
          f"{timings.get('індикатори + правила + гейт', 0):.3f} с "
          f"за {len(proposals)} пропозицій")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
