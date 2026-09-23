"""Render a route's price history as a PNG line chart, for ``/chart``.

Kept out of ``store.py``/``cli.py`` so importing matplotlib is only paid for
when a chart is actually requested, not on every scheduled tracking run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class ChartError(RuntimeError):
    """Nothing usable to plot, or matplotlib isn't installed."""


#: Every route name and axis label here is Chinese (see routes.yaml), and
#: matplotlib's bundled default font (DejaVu Sans) has no CJK glyphs at all --
#: without picking one of these, the title and axis labels silently render as
#: empty boxes. Checked in this order; the first one actually installed wins.
_CJK_FONT_CANDIDATES = (
    "Noto Sans CJK TC",
    "Noto Sans CJK SC",
    "Noto Sans TC",
    "Noto Sans SC",
    "WenQuanYi Zen Hei",
    "Microsoft JhengHei",
    "PingFang TC",
    "Heiti TC",
    "SimHei",
    "Droid Sans Fallback",
)


def _pick_cjk_font(font_manager) -> str | None:
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in _CJK_FONT_CANDIDATES:
        if name in available:
            return name
    return None


def render_price_trend(rows: list[dict], *, route_name: str, out_path: str | Path) -> Path:
    """Plot ``price`` over ``fetched_at`` for one route's history rows.

    ``rows`` is what :meth:`tracker.store.PriceStore.route_history` returns:
    one row per run, cheapest quote of that run. Needs at least two points --
    a single dot is not a trend.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless: no display in a GitHub Actions runner
        import matplotlib.dates as mdates
        import matplotlib.font_manager as font_manager
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - import guard
        raise ChartError('matplotlib 未安裝，請執行 pip install -e ".[chart]"') from exc

    cjk_font = _pick_cjk_font(font_manager)
    if cjk_font:
        plt.rcParams["font.sans-serif"] = [cjk_font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    times: list[datetime] = []
    prices: list[int] = []
    currency = ""
    for row in rows:
        try:
            when = datetime.fromisoformat(row["fetched_at"])
            price = int(row["price"])
        except (ValueError, KeyError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        times.append(when)
        prices.append(price)
        currency = row.get("currency", currency)

    if len(times) < 2:
        raise ChartError(f"[{route_name}] 歷史紀錄不到兩筆，還畫不出趨勢圖")

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    ax.plot(times, prices, marker="o", color="#1d4ed8", linewidth=2)
    ax.set_title(f"{route_name} 價格趨勢")
    ax.set_ylabel(f"價格（{currency}）" if currency else "價格")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path
