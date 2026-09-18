"""Command line entry point: ``run``, ``query``, ``report``, ``test-notify``."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

from .alerts import AlertState
from .config import Config, ConfigError, RouteConfig, Window, load_config
from .expand import TooManyQueries, drop_past, expand_all, expand_route, summarise
from .models import SearchOptions, SearchSpec
from .notify import available_notifiers, deliver, format_alerts
from .providers import ProviderError, get_provider
from .runner import run as run_searches
from .store import PriceStore

DEFAULT_CONFIG = "routes.yaml"
DEFAULT_PRICES = "data/prices.csv"
DEFAULT_STATE = "data/alerts.json"
DEFAULT_CURSOR = "data/command_cursor.json"


def _codes(raw: str) -> tuple[str, ...]:
    """Parse ``TPE,KHH`` into ``('TPE', 'KHH')``."""
    codes = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    if not codes:
        raise argparse.ArgumentTypeError(f"看不懂的機場代碼: {raw!r}")
    return codes


def _parse_depart_range(raw: str) -> tuple[date, ...]:
    """Parse ``2026-11-01:2026-11-30`` into every date in the span."""
    try:
        start_raw, end_raw = raw.split(":", 1)
        start, end = date.fromisoformat(start_raw.strip()), date.fromisoformat(end_raw.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--depart-range 格式應為 YYYY-MM-DD:YYYY-MM-DD，收到 {raw!r}") from exc
    if end < start:
        raise argparse.ArgumentTypeError(f"--depart-range 結束日 {end} 早於開始日 {start}")
    return tuple(date.fromordinal(o) for o in range(start.toordinal(), end.toordinal() + 1))


# ---------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"設定檔有問題：{exc}", file=sys.stderr)
        return 2

    try:
        specs = expand_all(config)
    except TooManyQueries as exc:
        print(str(exc), file=sys.stderr)
        return 2

    specs, past = drop_past(specs)
    if past:
        print(f"略過 {len(past)} 組出發日已過期的查詢（請更新 routes.yaml）", file=sys.stderr)

    print(summarise(specs), file=sys.stderr)
    if not specs:
        return 0

    if args.dry_run:
        print("\n--dry-run：以下查詢不會實際執行", file=sys.stderr)
        for spec in specs:
            print(f"  {spec.route_name}: {spec}")
        notifiers = available_notifiers()
        channels = ", ".join(n.name for n in notifiers) or "無（會退回 GitHub Issue，但目前也沒設定）"
        print(f"\n通知管道：{channels}", file=sys.stderr)
        return 0

    store = PriceStore(args.prices)
    state = AlertState(args.state)

    result = run_searches(specs, config, store, state, pause=not args.no_pause)
    print(f"\n{result.summary()}", file=sys.stderr)

    if result.alerts:
        subject, body = format_alerts(result.alerts)
        print(f"\n{subject}\n{body}")

        notifiers = available_notifiers()
        if not notifiers:
            print("沒有設定任何通知管道，只印在畫面上（見 README 設定 NTFY_TOPIC 或 LINE）", file=sys.stderr)
        for delivery in deliver(notifiers, subject, body):
            mark = "✓" if delivery.ok else "✗"
            print(f"  {mark} {delivery.channel} {delivery.detail}".rstrip(), file=sys.stderr)

        for alert in result.alerts:
            state.record(alert.quote)

    state.prune()
    state.save()

    if result.failures:
        print("\n失敗的查詢：", file=sys.stderr)
        for spec, reason in result.failures:
            print(f"  {spec}: {reason}", file=sys.stderr)

        # Partial failure is normal (a sold-out date, a flaky fetch). A total
        # wipeout means the tracker itself is broken -- most likely Google
        # changed something under fast-flights.
        if not result.quotes:
            # Say so on the phone, not just in a workflow log nobody reads.
            # A price tracker that quietly stops working looks exactly like a
            # tracker that is working and finding nothing cheap.
            _warn_tracker_is_broken(result)
            return 1

    return 0


def _warn_tracker_is_broken(result) -> None:
    """Push a failure notice to the same channels that carry the alerts."""
    reasons = []
    for _, reason in result.failures[:3]:
        reasons.append(f"· {reason}")
    if len(result.failures) > 3:
        reasons.append(f"· ...另外還有 {len(result.failures) - 3} 筆")

    subject = "⚠️ 機票追蹤器查不到任何票價"
    body = (
        f"{len(result.failures)} 組查詢全部失敗，這一輪沒有取得任何價格。\n"
        "通常代表 Google Flights 改版讓 fast-flights 失效了。\n\n"
        + "\n".join(reasons)
    )

    notifiers = available_notifiers()
    if not notifiers:
        return
    for delivery in deliver(notifiers, subject, body):
        mark = "✓" if delivery.ok else "✗"
        print(f"  {mark} 已告知 {delivery.channel} {delivery.detail}".rstrip(), file=sys.stderr)


def cmd_query(args: argparse.Namespace) -> int:
    """Ad-hoc search without touching routes.yaml."""
    if args.depart_range:
        departs = args.depart_range
    elif args.depart:
        departs = (args.depart,)
    else:
        departs = (date.today() + timedelta(days=30),)
        print(f"未指定出發日，預設用 30 天後（{departs[0]}）", file=sys.stderr)

    trip = "oneway" if args.oneway else "round"
    route = RouteConfig(
        name="臨時查詢",
        origins=args.origin,
        destinations=args.destination,
        trip=trip,
        windows=(Window(departs=departs, nights=None if args.oneway else args.nights),),
        options=SearchOptions(
            adults=args.adults,
            seat=args.seat,
            max_stops=args.max_stops,
            currency=args.currency,
        ),
    )

    specs = expand_route(route)
    if not specs:
        print("展開後沒有任何查詢（出發地和目的地是不是一樣？）", file=sys.stderr)
        return 2

    print(summarise(specs), file=sys.stderr)
    if args.dry_run:
        for spec in specs:
            print(f"  {spec}")
        return 0

    providers = ("fast_flights",) + (("serpapi",) if os.environ.get("SERPAPI_KEY") else ())
    config = Config(routes=(route,), currency=args.currency, providers=providers)
    store = PriceStore(args.prices) if args.save else PriceStore(os.devnull)
    state = AlertState(os.devnull)

    result = run_searches(specs, config, store, state, pause=not args.no_pause, keep_per_search=args.top)

    if not result.quotes:
        print("\n沒有查到任何票價。", file=sys.stderr)
        for spec, reason in result.failures:
            print(f"  {spec}: {reason}", file=sys.stderr)
        return 1

    print()
    _print_quote_table(sorted(result.quotes, key=lambda q: q.price))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    store = PriceStore(args.prices)
    since = date.today() - timedelta(days=args.days)

    # For a comparison group the question is "where is cheapest", so collapse
    # the date sweep to one row per destination. Otherwise show each itinerary.
    by_pair = bool(args.group) and not args.all_dates
    rows = (
        store.cheapest_per_pair(group=args.group, since=since)
        if by_pair
        else store.cheapest_per_key(group=args.group, since=since)
    )

    if not rows:
        target = f"群組 {args.group!r}" if args.group else "任何路線"
        print(f"近 {args.days} 天沒有 {target} 的價格紀錄。", file=sys.stderr)
        return 1

    header = f"近 {args.days} 天最低價"
    if args.group:
        header += f"（群組：{args.group}，每個目的地取最低）" if by_pair else f"（群組：{args.group}）"
    print(header)
    print("─" * 60)

    shown = rows[: args.limit]
    width = max(len(r["itinerary"]) for r in shown)
    for rank, row in enumerate(shown, start=1):
        dates = row["depart"] + (f"~{row['ret']}" if row["ret"] else "")
        print(
            f"{rank:>2}. {row['itinerary']:<{width}}  {int(row['price']):>8,} {row['currency']}"
            f"  {dates}  {row['airlines'] or '?'}"
        )
    return 0


def _command_source():
    """Pick which channel to poll for inbound commands.

    Telegram wins when both are configured, since it is the channel this
    project now recommends. Either way the same shared secret gates every
    command -- Telegram's bot token is already private, but a message still
    has to open with the secret before ``commands.parse`` accepts it, so a
    leaked token alone is not enough to drive the tracker.
    """
    secret = os.environ.get("COMMAND_SECRET") or os.environ.get("NTFY_COMMAND_SECRET", "")

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if telegram_token:
        from .notify.telegram import poll_updates

        return "telegram", (lambda cursor: poll_updates(telegram_token, since=cursor.last_id)), secret

    ntfy_topic = os.environ.get("NTFY_COMMAND_TOPIC", "")
    if ntfy_topic:
        from .notify.ntfy import poll_messages

        server = os.environ.get("NTFY_SERVER")
        return "ntfy", (lambda cursor: poll_messages(ntfy_topic, server=server, since=cursor.since)), secret

    return None, None, secret


def cmd_commands(args: argparse.Namespace) -> int:
    """Poll the configured command channel and apply whatever was sent."""
    from .commands import Cursor, process

    channel, poll, secret = _command_source()
    if channel is None:
        print(
            "沒有設定指令頻道（TELEGRAM_BOT_TOKEN，或 NTFY_COMMAND_TOPIC），沒有指令可讀。",
            file=sys.stderr,
        )
        return 0
    if not secret:
        # Without a shared secret, a leaked bot token or a guessed ntfy topic
        # name would be enough on its own. Refuse rather than accept commands
        # from anyone who gets hold of either.
        print("COMMAND_SECRET（或 NTFY_COMMAND_SECRET）未設定，為安全起見不處理任何指令。", file=sys.stderr)
        return 2

    cursor = Cursor(args.cursor)
    try:
        messages = poll(cursor)
    except Exception as exc:  # noqa: BLE001 - a poll failure is not worth failing the job
        print(f"讀取指令頻道失敗（{channel}）：{exc}", file=sys.stderr)
        return 1

    handled = process(messages, config_path=args.config, secret=secret, cursor=cursor)
    cursor.save()

    if not handled:
        print(f"沒有新指令（讀了 {len(messages)} 則訊息，來源：{channel}）", file=sys.stderr)
        return 0

    replies = []
    run_now = report_now = False
    for item in handled:
        print(f"/{item.command.verb} → {item.outcome.message or '(執行)'}", file=sys.stderr)
        if item.outcome.message:
            replies.append(item.outcome.message)
        run_now |= item.outcome.run_now
        report_now |= item.outcome.report_now

    if report_now:
        store = PriceStore(args.prices)
        rows = store.cheapest_per_pair(since=date.today() - timedelta(days=30))[:5]
        if rows:
            lines = [
                f"{i}. {r['itinerary']} {int(r['price']):,} {r['currency']} {r['depart']}"
                for i, r in enumerate(rows, start=1)
            ]
            replies.append("近 30 天最低價：\n" + "\n".join(lines))
        else:
            replies.append("還沒有價格紀錄。")

    # Sent through every configured notify channel, not just the one the
    # command arrived on -- someone running LINE + Telegram together should
    # see the result either way, the same as a price alert would reach both.
    if replies:
        for delivery in deliver(available_notifiers(), "✈️ 指令結果", "\n\n".join(replies)):
            if not delivery.ok:
                print(f"回覆失敗（指令已套用）：{delivery.channel} {delivery.detail}", file=sys.stderr)

    # Signal the workflow to run a tracking pass now instead of waiting for cron.
    if run_now:
        step_output = os.environ.get("GITHUB_OUTPUT")
        if step_output:
            with open(step_output, "a", encoding="utf-8") as handle:
                handle.write("run_now=true\n")
        print("已要求立刻查一輪", file=sys.stderr)

    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    """Send a test message to every configured channel."""
    notifiers = available_notifiers()
    if not notifiers:
        print(
            "沒有偵測到任何通知管道。請設定 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID、NTFY_TOPIC，"
            "或 LINE_CHANNEL_TOKEN + LINE_USER_ID（在 Actions 內沒設時會退回 GitHub Issue）。",
            file=sys.stderr,
        )
        return 1

    print(f"偵測到管道：{', '.join(n.name for n in notifiers)}", file=sys.stderr)
    subject = "✈️ 機票追蹤器測試訊息"
    body = f"這是一則測試訊息，送出時間 {datetime.now().isoformat(timespec='seconds')}。\n收到代表通知設定正確。"

    failed = False
    for delivery in deliver(notifiers, subject, body):
        mark = "✓" if delivery.ok else "✗"
        print(f"  {mark} {delivery.channel} {delivery.detail}".rstrip())
        failed |= not delivery.ok
    return 1 if failed else 0


def cmd_url(args: argparse.Namespace) -> int:
    """Print the Google Flights link for a search without fetching prices."""
    route = RouteConfig(
        name="連結",
        origins=args.origin,
        destinations=args.destination,
        trip="oneway" if args.oneway else "round",
        windows=(Window(departs=(args.depart,), nights=None if args.oneway else args.nights),),
        options=SearchOptions(adults=args.adults, seat=args.seat, currency=args.currency),
    )
    provider = get_provider("fast_flights")
    for spec in expand_route(route):
        try:
            print(f"{spec}\n  {provider.booking_url(spec)}")
        except ProviderError as exc:
            print(f"{spec}\n  ✗ {exc}", file=sys.stderr)
            return 1
    return 0


def _print_quote_table(quotes) -> None:
    width = max(len(q.itinerary) for q in quotes)
    for quote in quotes:
        print(
            f"{quote.price:>8,} {quote.currency}  {quote.itinerary:<{width}}"
            f"  {quote.date_label}  轉機 {quote.stops}  {quote.duration_label}  {quote.airline_label}"
        )
    cheapest = quotes[0]
    if cheapest.url:
        print(f"\n最低價連結：{cheapest.url}")


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flight-tracker",
        description="追蹤設定好的航線票價，發現便宜就通知。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="依 routes.yaml 跑完整一輪")
    p_run.add_argument("--config", default=DEFAULT_CONFIG)
    p_run.add_argument("--prices", default=DEFAULT_PRICES)
    p_run.add_argument("--state", default=DEFAULT_STATE)
    p_run.add_argument("--dry-run", action="store_true", help="只列出會查什麼，不連網、不寫檔、不通知")
    p_run.add_argument("--no-pause", action="store_true", help="不要在查詢之間等待（測試用，容易被擋）")
    p_run.set_defaults(func=cmd_run)

    # query
    p_query = sub.add_parser("query", help="臨時查一次，不用改設定檔")
    p_query.add_argument("origin", type=_codes, help="出發地，多個用逗號分隔，例如 TPE,KHH")
    p_query.add_argument("destination", type=_codes, help="目的地，多個用逗號分隔，例如 NRT,KIX,FUK")
    p_query.add_argument("--depart", type=date.fromisoformat, help="出發日 YYYY-MM-DD")
    p_query.add_argument("--depart-range", type=_parse_depart_range, help="出發日區間 YYYY-MM-DD:YYYY-MM-DD")
    p_query.add_argument("--nights", type=int, default=7, help="幾晚後回程（預設 7）")
    p_query.add_argument("--oneway", action="store_true", help="單程")
    p_query.add_argument("--adults", type=int, default=1)
    p_query.add_argument("--seat", default="economy", choices=["economy", "premium-economy", "business", "first"])
    p_query.add_argument("--max-stops", type=int, default=None)
    p_query.add_argument("--currency", default="TWD")
    p_query.add_argument("--top", type=int, default=3, help="每組行程顯示幾筆（預設 3）")
    p_query.add_argument("--save", action="store_true", help="把結果也寫進價格歷史")
    p_query.add_argument("--prices", default=DEFAULT_PRICES)
    p_query.add_argument("--dry-run", action="store_true", help="只列出會查什麼，不連網")
    p_query.add_argument("--no-pause", action="store_true")
    p_query.set_defaults(func=cmd_query)

    # report
    p_report = sub.add_parser("report", help="從歷史紀錄印出最低價排名")
    p_report.add_argument("--prices", default=DEFAULT_PRICES)
    p_report.add_argument("--group", default=None, help="只看某個 compare 群組，並依目的地取最低價排名")
    p_report.add_argument("--all-dates", action="store_true", help="群組模式下改為逐個出發日列出")
    p_report.add_argument("--days", type=int, default=30)
    p_report.add_argument("--limit", type=int, default=20)
    p_report.set_defaults(func=cmd_report)

    # commands
    p_commands = sub.add_parser("commands", help="讀取 ntfy 指令頻道，套用你從手機發的指令")
    p_commands.add_argument("--config", default=DEFAULT_CONFIG)
    p_commands.add_argument("--cursor", default=DEFAULT_CURSOR)
    p_commands.add_argument("--prices", default=DEFAULT_PRICES)
    p_commands.set_defaults(func=cmd_commands)

    # test-notify
    p_notify = sub.add_parser("test-notify", help="對所有已設定的通知管道送一則測試訊息")
    p_notify.set_defaults(func=cmd_test_notify)

    # url
    p_url = sub.add_parser("url", help="只印出 Google Flights 連結，不查價")
    p_url.add_argument("origin", type=_codes)
    p_url.add_argument("destination", type=_codes)
    p_url.add_argument("--depart", type=date.fromisoformat, required=True)
    p_url.add_argument("--nights", type=int, default=7)
    p_url.add_argument("--oneway", action="store_true")
    p_url.add_argument("--adults", type=int, default=1)
    p_url.add_argument("--seat", default="economy", choices=["economy", "premium-economy", "business", "first"])
    p_url.add_argument("--currency", default="TWD")
    p_url.set_defaults(func=cmd_url)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中斷。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
