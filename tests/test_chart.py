"""Price-trend PNG rendering for /chart.

No pixel inspection here -- that would just pin matplotlib's rendering
details. What's actually worth testing: a chart needs at least two points to
be a trend, bad/missing fields are skipped rather than crashing, and the file
actually lands on disk with real bytes in it.
"""

from __future__ import annotations

import pytest

from tracker.chart import ChartError, _pick_cjk_font, render_price_trend


def rows(*prices_and_days: tuple[int, int], currency: str = "TWD") -> list[dict]:
    return [
        {"fetched_at": f"2026-09-{day:02d}T01:00:00+00:00", "price": str(price), "currency": currency}
        for price, day in prices_and_days
    ]


def test_fewer_than_two_points_is_refused(tmp_path):
    with pytest.raises(ChartError, match="不到兩筆"):
        render_price_trend(rows((9000, 1)), route_name="台北-東京", out_path=tmp_path / "chart.png")


def test_no_history_at_all_is_refused(tmp_path):
    with pytest.raises(ChartError, match="不到兩筆"):
        render_price_trend([], route_name="台北-東京", out_path=tmp_path / "chart.png")


def test_two_points_renders_a_real_png_file(tmp_path):
    out = tmp_path / "chart.png"

    path = render_price_trend(rows((9000, 1), (8500, 5)), route_name="台北-東京", out_path=out)

    assert path == out
    assert out.exists()
    assert out.stat().st_size > 0
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "a real PNG, not an empty or corrupt file"


def test_rows_with_unparseable_fields_are_skipped_not_fatal(tmp_path):
    bad = [
        {"fetched_at": "not-a-date", "price": "9000", "currency": "TWD"},
        {"fetched_at": "2026-09-01T01:00:00+00:00", "price": "not-a-number", "currency": "TWD"},
    ]
    good = rows((9000, 1), (8500, 5))

    # Two good rows plus junk that must not crash the whole render.
    path = render_price_trend(bad + good, route_name="台北-東京", out_path=tmp_path / "chart.png")

    assert path.exists()


def test_one_good_row_among_junk_still_refuses(tmp_path):
    bad = [{"fetched_at": "not-a-date", "price": "9000", "currency": "TWD"}]
    with pytest.raises(ChartError, match="不到兩筆"):
        render_price_trend(bad + rows((8500, 5)), route_name="台北-東京", out_path=tmp_path / "chart.png")


def test_pick_cjk_font_returns_the_first_available_candidate():
    class FakeFont:
        def __init__(self, name):
            self.name = name

    class FakeFontManagerModule:
        class fontManager:
            ttflist = [FakeFont("DejaVu Sans"), FakeFont("WenQuanYi Zen Hei")]

    assert _pick_cjk_font(FakeFontManagerModule) == "WenQuanYi Zen Hei"


def test_pick_cjk_font_returns_none_when_nothing_matches():
    class FakeFont:
        def __init__(self, name):
            self.name = name

    class FakeFontManagerModule:
        class fontManager:
            ttflist = [FakeFont("DejaVu Sans")]

    assert _pick_cjk_font(FakeFontManagerModule) is None
