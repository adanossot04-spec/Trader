"""Замір: з чого складається час роботи бота — тепер із шаром глибини.

Запуск:
    python3 tools/timing.py            # усі тікери зі списку
    python3 tools/timing.py 5          # перші п'ять

Фази (саме в тому порядку, як у щоденному прогоні):
  0) свічки — мережа (Yahoo, 420 добових барів на тікер);
  1) контекст глибини — режим ринку (VIX, 10Y, DXY), ширина списку, календар звітів;
  2) прогрів SEC + RSS — фундаментал (SEC EDGAR) і заголовки (Yahoo RSS) паралельно;
  3) пакети глибини — фундаментальні показники, історія правила (бектест), оцінки;
  4) індикатори + правила + гейт — чиста математика на 420 свічках на тікер;
  5) сторінка результатів — HTML-збірка.

Міряємо ДВІ конфігурації: холодний кеш (перший запуск доби) і теплий (той самий
день удруге — так буде в кожному наступному прогінi). Кеш SEC/макро/RSS живе добу.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app import depth as depth_module              # noqa: E402
from app import market as market_module            # noqa: E402
from app import pipeline                           # noqa: E402
from app.db import Store                           # noqa: E402
from app.settings import load_settings             # noqa: E402
from make_site import build_site                   # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    settings = load_settings()
    tickers = settings.watchlist
    if argv and argv[0].isdigit():
        tickers = tickers[: int(argv[0])]

    store = Store(settings.db_path)
    store.seed_watchlist(settings.watchlist)
    requests = 0
    timings: dict[str, float] = {}

    def phase(name: str, fn):
        step = time.perf_counter()
        out = fn()
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - step)
        return out

    # ── холодний прогін ──────────────────────────────────────────────────────
    def fetch_all() -> dict:
        nonlocal requests
        bars: dict = {}
        for ticker in tickers:
            try:
                data = market_module.fetch_daily(ticker, settings, force_refresh=True)
                requests += 1
                if data and data.bars:
                    bars[ticker.upper()] = data.bars
            except Exception as exc:                                  # noqa: BLE001
                print(f"  {ticker}: не вдалося ({exc})")
        return bars

    print("→ холодний кеш (перший запуск доби)")
    started = time.perf_counter()
    bars_map = phase("0. свічки (мережа)", fetch_all)

    def context():
        return depth_module.build_context(settings, ticker_bars=bars_map)

    try:
        context_data = phase("1. контекст глибини (макро+календар)", context)
    except Exception as exc:                                          # noqa: BLE001
        print(f"  контекст глибини недоступний: {exc}")
        context_data = {"macro": {}, "calendar": {}, "spy_bars": None, "breadth_pct": None}

    phase("2. прогрів SEC + RSS (паралельно)",
          lambda: depth_module.warm(settings, [t.upper() for t in tickers], context_data.get("calendar")))

    def packets():
        out = []
        for ticker in tickers:
            out.append(depth_module.collect(
                ticker, settings, bars=bars_map.get(ticker.upper(), []),
                spy_bars=context_data.get("spy_bars"), macro=context_data.get("macro"),
                calendar=context_data.get("calendar"),
            ))
        return out

    cold_packets = phase("3. пакети глибини (рахунок)", packets)

    def proposals():
        return pipeline.build_proposals(settings, store, notify=False, summary=False)

    made = phase("4. індикатори + правила + гейт (з глибиною)", proposals)
    cold_total = time.perf_counter() - started

    phase("5. збірка сторінки результатів",
          lambda: build_site(settings=settings, out_dir=Path("site"), sessions=10, pages_for_sessions=1))

    cold = dict(timings)
    timings = {key: 0.0 for key in timings}
    requests_cold = requests

    # ── теплий прогін (усе в кеші — так виглядає другий запуск того ж дня) ────
    print("→ теплий кеш (той самий день, повторний прогін)")
    started = time.perf_counter()
    phase("4. індикатори + правила + гейт (з глибиною)", proposals)
    warm_total = time.perf_counter() - started

    def report(title: str, values: dict[str, float], total: float) -> None:
        print(f"\n{title}")
        print(f"{'фаза':44} {'секунд':>9} {'частка':>8}")
        print("-" * 64)
        for name, seconds in sorted(values.items(), key=lambda kv: -kv[1]):
            if seconds <= 0:
                continue
            print(f"{name:44} {seconds:9.2f} {100 * seconds / total:7.1f}%")
        print("-" * 64)
        print(f"{'РАЗОМ':44} {total:9.2f} {100:7.1f}%")

    bars_count = len(next(iter(bars_map.values()), []))
    print(f"\nТікерів: {len(tickers)} · HTTP-запитів (свічки): {requests_cold} · свічок на тікер: {bars_count}")
    print(f"Пропозицій у прогоні: {len(made)} · пакетів глибини: {len(cold_packets)}")
    report("ХОЛОДНИЙ КЕШ (секунди)", cold, cold_total)
    report("ТЕПЛИЙ КЕШ (секунди)", timings, warm_total)

    math_ms = 1000 * cold.get("3. пакети глибини (рахунок)", 0) / max(1, len(tickers))
    print(f"\nЧистий рахунок глибини (без мережі): {math_ms:.1f} мс на тікер "
          f"({len(tickers)} тікерів за {cold.get('3. пакети глибини (рахунок)', 0):.2f} с)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
