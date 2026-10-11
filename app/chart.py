"""SVG-графіки: добові свічки з рівнями пропозиції та спарклайни. Без залежностей і без мережі."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

UP = "#3fb950"
DOWN = "#f85149"
SMA20 = "#f0b429"
SMA50 = "#58a6ff"
ENTRY = "#1f6feb"
STOP = "#f85149"
TARGET = "#3fb950"
GRID = "#21262d"
TEXT = "#8b949e"


def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 100:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def candles_svg(
    bars: Sequence[Any],
    *,
    levels: dict[str, float | None] | None = None,
    sma20: Sequence[float | None] | None = None,
    sma50: Sequence[float | None] | None = None,
    title: str = "",
    width: int = 820,
    height: int = 400,
) -> str:
    """Свічки останніх N сесій + рівні входу/стопу/цілі. Усе inline, без зовнішніх ресурсів."""
    if not bars:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'

    pad_left, pad_right, pad_top, pad_bottom = 8, 62, 26, 18
    price_h = (height - pad_top - pad_bottom) * 0.76
    vol_h = (height - pad_top - pad_bottom) * 0.24
    plot_w = width - pad_left - pad_right
    vol_top = pad_top + price_h + 12

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    level_values = [v for v in (levels or {}).values() if v]
    y_min = min(lows + level_values) if level_values else min(lows)
    y_max = max(highs + level_values) if level_values else max(highs)
    for series in (sma20, sma50):
        if series:
            clean = [v for v in series if v is not None]
            if clean:
                y_min = min(y_min, min(clean))
                y_max = max(y_max, max(clean))
    span = max(y_max - y_min, 1e-6)
    y_min -= span * 0.04
    y_max += span * 0.04
    span = y_max - y_min

    def y_of(price: float) -> float:
        return pad_top + (y_max - price) / span * price_h

    n = len(bars)
    step = plot_w / max(n, 1)
    body_w = max(1.2, min(step * 0.62, 12.0))
    max_vol = max((b.volume for b in bars), default=1) or 1

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" '
        f'style="max-width:{width}px;background:#0d1117;border:1px solid #21262d;border-radius:10px" '
        f'role="img" aria-label="{title}">'
    ]
    if title:
        parts.append(
            f'<text x="{pad_left + 6}" y="17" fill="#c9d1d9" font-size="12.5" '
            f'font-family="ui-monospace, monospace">{title}</text>'
        )

    # сітка + цінові підписи
    for i in range(5):
        price = y_max - span * i / 4
        y = y_of(price)
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + plot_w}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(
            f'<text x="{width - pad_right + 6}" y="{y + 3.5:.1f}" fill="{TEXT}" font-size="10.5" '
            f'font-family="ui-monospace, monospace">{_fmt_price(price)}</text>'
        )

    # свічки
    for idx, bar in enumerate(bars):
        cx = pad_left + step * (idx + 0.5)
        up = bar.close >= bar.open
        color = UP if up else DOWN
        y_high, y_low = y_of(bar.high), y_of(bar.low)
        y_open, y_close = y_of(bar.open), y_of(bar.close)
        top, bottom = min(y_open, y_close), max(y_open, y_close)
        parts.append(f'<line x1="{cx:.1f}" y1="{y_high:.1f}" x2="{cx:.1f}" y2="{y_low:.1f}" stroke="{color}" stroke-width="1"/>')
        parts.append(
            f'<rect x="{cx - body_w / 2:.1f}" y="{top:.1f}" width="{body_w:.1f}" '
            f'height="{max(bottom - top, 1.0):.1f}" fill="{color}" opacity="0.95"/>'
        )
        vh = (bar.volume / max_vol) * vol_h
        parts.append(
            f'<rect x="{cx - body_w / 2:.1f}" y="{vol_top + vol_h - vh:.1f}" width="{body_w:.1f}" '
            f'height="{max(vh, 0.6):.1f}" fill="{color}" opacity="0.45"/>'
        )

    # ковзні середні
    for series, color, label in ((sma20, SMA20, "SMA20"), (sma50, SMA50, "SMA50")):
        if not series:
            continue
        points = [
            f"{pad_left + step * (idx + 0.5):.1f},{y_of(value):.1f}"
            for idx, value in enumerate(series)
            if value is not None and idx < n
        ]
        if len(points) > 1:
            parts.append(
                f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="1.3" opacity="0.9"/>'
            )
    legend_x = pad_left + 6
    if sma20:
        parts.append(f'<rect x="{legend_x}" y="{height - 12}" width="10" height="3" fill="{SMA20}"/>')
        parts.append(f'<text x="{legend_x + 14}" y="{height - 8}" fill="{TEXT}" font-size="10">SMA20</text>')
    if sma50:
        parts.append(f'<rect x="{legend_x + 62}" y="{height - 12}" width="10" height="3" fill="{SMA50}"/>')
        parts.append(f'<text x="{legend_x + 76}" y="{height - 8}" fill="{TEXT}" font-size="10">SMA50</text>')

    # рівні входу/стопу/цілі
    for key, color, label in (("entry", ENTRY, "Вхід"), ("stop", STOP, "Стоп"), ("target", TARGET, "Ціль")):
        price = (levels or {}).get(key)
        if not price:
            continue
        y = y_of(float(price))
        if not (pad_top - 6 <= y <= pad_top + price_h + 6):
            continue
        dash = "6 4"
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + plot_w}" y2="{y:.1f}" stroke="{color}" '
            f'stroke-width="1.2" stroke-dasharray="{dash}" opacity="0.95"/>'
        )
        parts.append(
            f'<text x="{pad_left + 6}" y="{y - 4:.1f}" fill="{color}" font-size="10.5" '
            f'font-family="ui-monospace, monospace">{label} {_fmt_price(float(price))}</text>'
        )

    first, last = bars[0].date, bars[-1].date
    parts.append(f'<text x="{pad_left + 6}" y="{pad_top + price_h + 10}" fill="{TEXT}" font-size="10">{first}</text>')
    parts.append(
        f'<text x="{pad_left + plot_w - 62}" y="{pad_top + price_h + 10}" fill="{TEXT}" font-size="10">{last}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def sparkline_svg(values: Sequence[float], *, width: int = 120, height: int = 30, color: str | None = None) -> str:
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 2:
        return ""
    lo, hi = min(clean), max(clean)
    span = (hi - lo) or 1.0
    step = width / (len(clean) - 1)
    points = " ".join(
        f"{idx * step:.1f},{height - 2 - (value - lo) / span * (height - 6):.1f}" for idx, value in enumerate(clean)
    )
    stroke = color or (UP if clean[-1] >= clean[0] else DOWN)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        f'<polyline points="{points}" fill="none" stroke="{stroke}" stroke-width="1.6"/></svg>'
    )


def levels_from_proposal(proposal: Any) -> dict[str, float | None]:
    return {
        "entry": getattr(proposal, "entry_price", None),
        "stop": getattr(proposal, "stop_price", None),
        "target": getattr(proposal, "target_price", None),
    }


def last_closes(bars: Iterable[Any], n: int = 30) -> list[float]:
    return [b.close for b in list(bars)[-n:]]
