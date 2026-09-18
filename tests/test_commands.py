"""The ntfy command channel: parsing, authorisation, edits, and guardrails.

These matter more than they look. The channel accepts instructions from the
open internet (anyone who learns a topic name can publish to it), and it
rewrites the file that decides how many queries get fired at Google.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tracker.commands import Command, CommandError, Cursor, apply, parse, process
from tracker.config import load_config
from tracker.expand import expand_all

SECRET = "hunter2"
REPO_ROOT = Path(__file__).parent.parent


# A fixed config of the shape these tests reason about: one multi-destination
# route carrying an inline comment and a flow list, plus a multi-city route.
# Deliberately NOT the shipped routes.yaml -- that file exists to be edited, and
# tests pinned to its contents break every time someone changes where they fly.
TEST_CONFIG = """\
currency: TWD
max_queries: 60
baseline_days: 30

defaults:
  adults: 1
  seat: economy
  max_stops: 2

routes:
  - name: 台北-澳洲東岸
    from: TPE
    to: [SYD, OOL]
    trip: round
    compare: true            # 報表會把兩個目的地排名
    windows:
      - depart_range: [2026-12-12, 2026-12-26]   # ← 改成你的出發區間
        nights: 12
    alert_below: 26000
    alert_drop_pct: 12

  - name: 雪梨進黃金海岸出
    trip: multi
    legs:
      - {from: TPE, to: SYD, depart: 2026-12-12}
      - {from: OOL, to: TPE, depart: 2026-12-24}
    alert_below: 30000
"""


@pytest.fixture
def config(tmp_path) -> Path:
    path = tmp_path / "routes.yaml"
    path.write_text(TEST_CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def shipped_config(tmp_path) -> Path:
    """A copy of the real routes.yaml, for smoke tests only."""
    path = tmp_path / "shipped.yaml"
    shutil.copy(REPO_ROOT / "routes.yaml", path)
    return path


def run(text: str, config: Path):
    command = parse(text, secret=SECRET)
    assert command is not None, f"{text!r} did not parse as a command"
    return apply(command, config)


# ---------------------------------------------------------------- authorisation


def test_a_message_without_the_secret_is_ignored():
    assert parse("/add BNE", secret=SECRET) is None


def test_a_message_with_the_wrong_secret_is_ignored():
    assert parse("letmein /add BNE", secret=SECRET) is None


def test_no_configured_secret_means_nothing_is_accepted():
    """Fail closed: the topic name alone must never be enough to drive this."""
    assert parse("/add BNE", secret="") is None
    assert parse("hunter2 /add BNE", secret="") is None


def test_ordinary_chatter_is_not_a_command():
    assert parse("hunter2 記得買機票", secret=SECRET) is None
    assert parse("hello", secret=SECRET) is None


# ---------------------------------------------------------------- parsing


def test_verb_and_arguments_are_split():
    command = parse("hunter2 /scan 2027-03-01 2027-03-20 14", secret=SECRET)

    assert command.verb == "scan"
    assert command.args == ("2027-03-01", "2027-03-20", "14")
    assert command.target is None


def test_route_can_be_targeted_with_an_at_sign():
    command = parse("hunter2 /add BNE @台北-澳洲東岸", secret=SECRET)

    assert command.verb == "add"
    assert command.args == ("BNE",)
    assert command.target == "台北-澳洲東岸"


def test_verbs_are_case_insensitive_and_whitespace_tolerant():
    command = parse("  hunter2   /ADD   bne  ", secret=SECRET)

    assert command.verb == "add"
    assert command.args == ("bne",)


# ---------------------------------------------------------------- edits


def test_add_appends_a_destination(config):
    outcome = run("hunter2 /add BNE", config)

    assert outcome.changed
    assert "BNE" in outcome.message
    assert "BNE" in load_config(config).routes[0].destinations


def test_add_preserves_comments_and_inline_list_style(config):
    before = config.read_text(encoding="utf-8")
    run("hunter2 /add BNE", config)
    after = config.read_text(encoding="utf-8")

    assert "to: [SYD, OOL, BNE]" in after, "stays an inline list"
    assert "# ← 改成你的出發區間" in after, "inline comments survive"
    # Exactly one line differs: the edit, and nothing else reformatted.
    changed = [
        (a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b
    ]
    assert len(changed) == 1


def test_add_rejects_things_that_are_not_airport_codes(config):
    for bad in ("SYDNEY", "S", "12", "雪梨"):
        with pytest.raises(CommandError, match="不像機場代碼"):
            run(f"hunter2 /add {bad}", config)


def test_add_rejects_a_duplicate(config):
    with pytest.raises(CommandError, match="已經在追了"):
        run("hunter2 /add SYD", config)


def test_remove_drops_a_destination(config):
    run("hunter2 /rm OOL", config)

    assert load_config(config).routes[0].destinations == ("SYD",)


def test_remove_refuses_to_empty_the_list(config):
    run("hunter2 /rm OOL", config)

    with pytest.raises(CommandError, match="最後一個目的地"):
        run("hunter2 /rm SYD", config)


def test_remove_rejects_something_not_tracked(config):
    with pytest.raises(CommandError, match="不在追蹤清單裡"):
        run("hunter2 /rm MEL", config)


# ---------------------------------------------------------------- /to and /from


def test_to_replaces_a_single_destination(config):
    """The exact gap /rm cannot close: swapping a route's one and only destination.

    routes.yaml's own comparison route has two destinations, so target the
    multi-city route's sibling isn't available here -- this exercises the
    single-destination case a real route (the one this command was built for)
    has, by first collapsing to one via /rm.
    """
    run("hunter2 /rm OOL", config)  # down to a single destination: SYD
    outcome = run("hunter2 /to BNE", config)

    assert outcome.changed
    assert load_config(config).routes[0].destinations == ("BNE",)


def test_to_replaces_multiple_destinations_at_once(config):
    run("hunter2 /to SYD OOL BNE", config)

    assert load_config(config).routes[0].destinations == ("SYD", "OOL", "BNE")


def test_to_writes_a_single_code_as_a_bare_string_not_a_list(config):
    run("hunter2 /rm OOL", config)
    run("hunter2 /to BNE", config)

    assert "to: BNE" in config.read_text(encoding="utf-8")
    assert "to: [BNE]" not in config.read_text(encoding="utf-8")


def test_to_rejects_bad_codes_and_changes_nothing(config):
    before = config.read_text(encoding="utf-8")

    with pytest.raises(CommandError, match="不像機場代碼"):
        run("hunter2 /to notacode", config)

    assert config.read_text(encoding="utf-8") == before


def test_to_requires_at_least_one_code(config):
    with pytest.raises(CommandError, match="至少要給一個機場代碼"):
        run("hunter2 /to", config)


def test_from_replaces_the_origin(config):
    run("hunter2 /from KHH", config)

    assert load_config(config).routes[0].origins == ("KHH",)


def test_from_accepts_multiple_origins(config):
    # Two origins x two existing destinations x a 15-day window would blow the
    # query budget, so narrow the destinations first -- exactly how a real
    # user would need to sequence it too.
    run("hunter2 /rm OOL", config)
    run("hunter2 /from TPE KHH", config)

    assert load_config(config).routes[0].origins == ("TPE", "KHH")


def test_to_and_from_refuse_multi_city_routes(config):
    with pytest.raises(CommandError, match="多段行程請直接改 legs"):
        run("hunter2 /to BNE @雪梨進黃金海岸出", config)

    with pytest.raises(CommandError, match="多段行程請直接改 legs"):
        run("hunter2 /from KHH @雪梨進黃金海岸出", config)


# ---------------------------------------------------------------- /rename


def test_rename_changes_the_route_name(config):
    outcome = run("hunter2 /rename 台北-布里斯本", config)

    assert outcome.changed
    assert "[台北-澳洲東岸]" in outcome.message
    names = [r.name for r in load_config(config).routes]
    assert "台北-布里斯本" in names
    assert "台北-澳洲東岸" not in names


def test_rename_rejects_a_name_already_in_use(config):
    with pytest.raises(CommandError, match="已經有路線叫"):
        run("hunter2 /rename 雪梨進黃金海岸出", config)


def test_rename_requires_exactly_one_argument(config):
    with pytest.raises(CommandError, match="用法"):
        run("hunter2 /rename", config)

    with pytest.raises(CommandError, match="用法"):
        run("hunter2 /rename 兩個 名字", config)


def test_a_renamed_route_can_then_be_targeted_by_its_new_name(config):
    run("hunter2 /rename 台北-布里斯本", config)

    outcome = run("hunter2 /price 20000 @台北-布里斯本", config)

    assert outcome.changed


def test_scan_sets_a_date_range(config):
    run("hunter2 /scan 2027-03-01 2027-03-14 12", config)

    window = load_config(config).routes[0].windows[0]
    assert window.departs[0].isoformat() == "2027-03-01"
    assert window.departs[-1].isoformat() == "2027-03-14"
    assert window.nights == 12


def test_scan_writes_dates_unquoted(config):
    run("hunter2 /scan 2027-03-01 2027-03-14 12", config)

    assert "depart_range: [2027-03-01, 2027-03-14]" in config.read_text(encoding="utf-8")


def test_scan_accepts_a_single_day(config):
    run("hunter2 /scan 2027-03-05 12", config)

    window = load_config(config).routes[0].windows[0]
    assert len(window.departs) == 1
    assert window.departs[0].isoformat() == "2027-03-05"


def test_scan_switching_from_range_to_single_day_removes_the_range(config):
    run("hunter2 /scan 2027-03-01 2027-03-14 12", config)
    run("hunter2 /scan 2027-03-05 12", config)

    text = config.read_text(encoding="utf-8")
    assert "depart_range" not in text.split("routes:")[1].split("- name: 雪梨")[0]
    assert len(load_config(config).routes[0].windows[0].departs) == 1


def test_scan_rejects_a_backwards_range(config):
    with pytest.raises(CommandError, match="早於起日"):
        run("hunter2 /scan 2027-03-20 2027-03-01 12", config)


def test_scan_rejects_a_bad_date(config):
    with pytest.raises(CommandError, match="不是 YYYY-MM-DD"):
        run("hunter2 /scan 3月1號 2027-03-14 12", config)


def test_scan_rejects_wrong_argument_counts(config):
    for args in ("", "2027-03-01", "2027-03-01 2027-03-14 12 extra"):
        with pytest.raises(CommandError, match="用法"):
            run(f"hunter2 /scan {args}".strip(), config)


def test_nights_changes_only_the_stay_length(config):
    run("hunter2 /nights 10", config)

    assert load_config(config).routes[0].windows[0].nights == 10


def test_price_and_drop_change_the_thresholds(config):
    run("hunter2 /price 24000", config)
    run("hunter2 /drop 15", config)

    route = load_config(config).routes[0]
    assert route.alert_below == 24000
    assert route.alert_drop_pct == 15


def test_price_rejects_nonsense(config):
    for bad in ("abc", "0", "-5"):
        with pytest.raises(CommandError):
            run(f"hunter2 /price {bad}", config)


def test_drop_rejects_a_percentage_of_100_or_more(config):
    with pytest.raises(CommandError, match="小於 100"):
        run("hunter2 /drop 100", config)


def test_stops_accepts_zero_for_nonstop_only(config):
    run("hunter2 /stops 0", config)

    assert load_config(config).routes[0].options.max_stops == 0


# ---------------------------------------------------------------- guardrails


def test_an_edit_that_blows_the_query_budget_is_refused(config):
    before = config.read_text(encoding="utf-8")

    with pytest.raises(CommandError, match="超過上限"):
        run("hunter2 /scan 2027-01-01 2027-12-31 12", config)

    assert config.read_text(encoding="utf-8") == before, "the file must be left untouched"


def test_the_refusal_reports_the_real_number(config):
    with pytest.raises(CommandError) as excinfo:
        run("hunter2 /scan 2027-01-01 2027-12-31 12", config)

    # 365 days x 2 destinations + 1 multi-city route.
    assert "731" in str(excinfo.value)


def test_every_applied_edit_leaves_a_loadable_config(config):
    for text in (
        "hunter2 /add BNE",
        "hunter2 /scan 2027-03-01 2027-03-10 12",
        "hunter2 /price 24000",
        "hunter2 /stops 1",
        "hunter2 /rm OOL",
    ):
        run(text, config)
        expand_all(load_config(config))


def test_an_unknown_verb_is_rejected(config):
    with pytest.raises(CommandError, match="不認得的指令"):
        run("hunter2 /destroy everything", config)


def test_a_missing_route_target_is_reported_with_the_real_names(config):
    with pytest.raises(CommandError, match="找不到路線"):
        run("hunter2 /add BNE @不存在的路線", config)


def test_commands_target_the_first_non_multi_route_by_default(config):
    """Adding a destination to a fixed multi-city itinerary is meaningless."""
    outcome = run("hunter2 /add BNE", config)

    assert "台北-澳洲東岸" in outcome.message


# ---------------------------------------------------------------- read-only verbs


def test_routes_lists_the_current_setup_without_changing_it(config):
    before = config.read_text(encoding="utf-8")
    outcome = run("hunter2 /routes", config)

    assert not outcome.changed
    assert "台北-澳洲東岸" in outcome.message
    assert "SYD" in outcome.message
    assert config.read_text(encoding="utf-8") == before


def test_help_lists_the_commands(config):
    outcome = run("hunter2 /help", config)

    assert "/scan" in outcome.message and "/add" in outcome.message
    assert "/to" in outcome.message and "/from" in outcome.message and "/rename" in outcome.message
    assert not outcome.changed


def test_run_asks_for_an_immediate_pass(config):
    outcome = run("hunter2 /run", config)

    assert outcome.run_now and not outcome.changed


def test_report_asks_for_a_report(config):
    assert run("hunter2 /report", config).report_now


def test_status_asks_for_a_status_reply(config):
    outcome = run("hunter2 /status", config)

    assert outcome.status_now
    assert not outcome.changed
    assert outcome.message == ""


def test_reset_asks_for_a_reset(config):
    outcome = run("hunter2 /reset", config)

    assert outcome.reset_now
    assert not outcome.changed
    assert outcome.message == ""


# ---------------------------------------------------------------- aliases


def test_time_is_an_alias_for_scan(config):
    run("hunter2 /time 2027-03-01 2027-03-14 12", config)

    window = load_config(config).routes[0].windows[0]
    assert window.departs[0].isoformat() == "2027-03-01"
    assert window.nights == 12


def test_location_is_an_alias_for_to(config):
    run("hunter2 /location SYD OOL BNE", config)

    assert load_config(config).routes[0].destinations == ("SYD", "OOL", "BNE")


# ---------------------------------------------------------------- /newroute and /delroute


def test_newroute_creates_a_whole_new_route(config):
    outcome = run("hunter2 /newroute 台北-福岡 TPE FUK 2027-06-05 7 20000", config)

    assert outcome.changed
    assert "新增路線" in outcome.message
    names = [r.name for r in load_config(config).routes]
    assert "台北-福岡" in names

    new_route = next(r for r in load_config(config).routes if r.name == "台北-福岡")
    assert new_route.origins == ("TPE",)
    assert new_route.destinations == ("FUK",)
    assert new_route.trip == "round"
    assert new_route.windows[0].nights == 7
    assert new_route.windows[0].departs[0].isoformat() == "2027-06-05"
    assert new_route.alert_below == 20000
    # inherits max_stops from top-level defaults rather than hardcoding one
    assert new_route.options.max_stops == 2


def test_newroute_without_a_threshold_is_optional(config):
    run("hunter2 /newroute 台北-福岡 TPE FUK 2027-06-05 7", config)

    new_route = next(r for r in load_config(config).routes if r.name == "台北-福岡")
    assert new_route.alert_below is None


def test_newroute_rejects_a_name_already_in_use(config):
    with pytest.raises(CommandError, match="已經有路線叫"):
        run("hunter2 /newroute 台北-澳洲東岸 TPE FUK 2027-06-05 7", config)


def test_newroute_rejects_bad_airport_codes(config):
    with pytest.raises(CommandError, match="不像機場代碼"):
        run("hunter2 /newroute 新路線 台北 FUK 2027-06-05 7", config)


def test_newroute_rejects_wrong_argument_counts(config):
    for args in ("台北-福岡 TPE FUK 2027-06-05", "台北-福岡 TPE FUK 2027-06-05 7 20000 extra"):
        with pytest.raises(CommandError, match="用法"):
            run(f"hunter2 /newroute {args}", config)


def test_newroute_reports_the_query_budget(config):
    outcome = run("hunter2 /newroute 台北-福岡 TPE FUK 2027-06-05 7", config)

    # One route, one fixed departure date, one destination: exactly one query.
    assert "共 32 次查詢" in outcome.message


def test_delroute_removes_a_route(config):
    outcome = run("hunter2 /delroute 雪梨進黃金海岸出", config)

    assert outcome.changed
    names = [r.name for r in load_config(config).routes]
    assert "雪梨進黃金海岸出" not in names
    assert len(names) == 1


def test_delroute_refuses_to_empty_the_config(config):
    run("hunter2 /delroute 雪梨進黃金海岸出", config)

    with pytest.raises(CommandError, match="最後一條路線"):
        run("hunter2 /delroute 台北-澳洲東岸", config)


def test_delroute_rejects_an_unknown_name(config):
    with pytest.raises(CommandError, match="找不到路線"):
        run("hunter2 /delroute 不存在的路線", config)


def test_delroute_requires_exactly_one_argument(config):
    with pytest.raises(CommandError, match="用法"):
        run("hunter2 /delroute", config)


# ---------------------------------------------------------------- batch processing


def message(text: str, message_id: str) -> dict:
    return {"event": "message", "id": message_id, "message": text}


def test_process_applies_messages_in_order(config, tmp_path):
    cursor = Cursor(tmp_path / "cursor.json")
    handled = process(
        [message("hunter2 /add BNE", "m1"), message("hunter2 /price 24000", "m2")],
        config_path=config,
        secret=SECRET,
        cursor=cursor,
    )

    assert [h.command.verb for h in handled] == ["add", "price"]
    assert all(h.outcome.ok for h in handled)
    assert cursor.last_id == "m2"


def test_process_skips_messages_that_are_not_commands(config, tmp_path):
    cursor = Cursor(tmp_path / "cursor.json")
    handled = process(
        [message("隨便聊聊", "m1"), message("wrongsecret /add BNE", "m2")],
        config_path=config,
        secret=SECRET,
        cursor=cursor,
    )

    assert handled == []
    assert cursor.last_id == "m2", "the cursor still advances past ignored messages"


def test_a_failing_command_is_reported_and_does_not_stop_the_batch(config, tmp_path):
    handled = process(
        [message("hunter2 /add NOPE", "m1"), message("hunter2 /add BNE", "m2")],
        config_path=config,
        secret=SECRET,
        cursor=Cursor(tmp_path / "cursor.json"),
    )

    assert not handled[0].outcome.ok
    assert "✗" in handled[0].outcome.message
    assert handled[1].outcome.ok


def test_the_cursor_advances_past_a_failed_command(config, tmp_path):
    """Otherwise the same error is re-sent on every poll, forever."""
    cursor = Cursor(tmp_path / "cursor.json")
    process([message("hunter2 /add NOPE", "m1")], config_path=config, secret=SECRET, cursor=cursor)

    assert cursor.last_id == "m1"


def test_the_cursor_round_trips_through_disk(tmp_path):
    path = tmp_path / "cursor.json"
    cursor = Cursor(path)
    cursor.record("m42")
    cursor.save()

    assert Cursor(path).last_id == "m42"
    assert Cursor(path).since == "m42"


def test_a_fresh_cursor_looks_back_one_poll_interval_not_all_history(tmp_path):
    assert Cursor(tmp_path / "missing.json").since == "30m"


def test_a_corrupt_cursor_does_not_stop_commands_working(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text("not json", encoding="utf-8")

    assert Cursor(path).since == "30m"


# ---------------------------------------------------------------- shipped config


def test_the_shipped_config_accepts_commands(shipped_config):
    """Whatever routes are currently tracked, the command channel must still drive them.

    Asserts on behaviour, not on the destinations -- routes.yaml is meant to change.
    """
    listing = run("hunter2 /routes", shipped_config)
    assert listing.message and not listing.changed

    before = shipped_config.read_text(encoding="utf-8")
    assert run("hunter2 /price 24000", shipped_config).changed
    expand_all(load_config(shipped_config))

    after = shipped_config.read_text(encoding="utf-8")
    changed = [
        (a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b
    ]
    assert len(changed) == 1, "a command must not reformat the file it edits"
