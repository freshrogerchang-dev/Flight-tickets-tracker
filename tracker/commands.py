"""Change the tracked routes by sending a message from your phone.

Commands arrive on a second ntfy topic, which a scheduled job polls. That keeps
the whole thing serverless -- ntfy is pub/sub in both directions, so no webhook
endpoint has to exist anywhere.

Parsing and applying are pure functions over text and a YAML path, so the whole
command surface is testable without a network or a real repository.

Two things guard against a stray or hostile message:

* every command must carry a shared secret, and a run with no secret configured
  refuses to process anything at all rather than accepting commands from
  whoever guessed the topic name;
* an edit is validated -- parsed, expanded, and checked against the query
  budget -- before it is allowed to overwrite ``routes.yaml``.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq

from .config import ConfigError, load_config
from .expand import TooManyQueries, expand_all

AIRPORT_CODE = re.compile(r"^[A-Z]{3}$")

HELP_TEXT = """可用指令：
/routes                     看目前設定
/status                     看設定＋每條路線最新查到的價格
/scan 起日 迄日 晚數        改掃描區間，例：/scan 2027-03-01 2027-03-20 14
/scan 出發日 晚數           只掃一天，例：/scan 2027-03-05 14
/time ...                   /scan 的別名（改日期，名字比較好記）
/nights 晚數                只改待幾晚
/add 機場代碼               加目的地，例：/add BNE
/rm 機場代碼                移除目的地
/to 機場代碼...             整個換掉目的地，例：/to BNE 或 /to SYD OOL BNE
/location 機場代碼...       /to 的別名（換目的地，名字比較好記）
/from 機場代碼...           整個換掉出發地，例：/from TPE KHH
/rename 新名稱              幫這條路線改名，例：/rename 台北-布里斯本
/newroute 名稱 出發地 目的地 出發日 晚數 [門檻]
                             新增一整條新路線，例：/newroute 台北-福岡 TPE FUK 2027-06-05 7 20000
/delroute 名稱              刪除一整條路線（不能刪到一條都不剩）
/price 金額                 改通知門檻，例：/price 24000
/drop 百分比                改跌價通知門檻，例：/drop 12
/stops 轉機次數上限         例：/stops 0（只要直飛）
/run                        立刻查一輪
/report                     看最低價排名
/reset                      清空所有已通知紀錄，讓之前通知過的低價下次符合門檻能再通知一次
/help                       這則說明

指令要加通關碼，格式：<通關碼> /add BNE
多條路線時用 @名稱 指定，例：/add BNE @台北-澳洲東岸
（/to /from /rename 對多段行程路線不生效，那種要直接改 legs）
（/newroute 只能建立來回行程；單程或多段行程要直接編輯 routes.yaml）"""


class CommandError(ValueError):
    """A command could not be understood or applied. The message is sent back to the user."""


@dataclass
class Command:
    verb: str
    args: tuple[str, ...] = ()
    #: Route name given as ``@名稱``; ``None`` means the default route.
    target: str | None = None
    raw: str = ""
    #: ntfy message id, used to advance the cursor.
    message_id: str = ""


@dataclass
class CommandOutcome:
    ok: bool
    message: str
    changed: bool = False
    #: Set by ``/run``, ``/report``, ``/status`` and ``/reset``, which the CLI
    #: acts on after applying edits -- each needs data (price history, alert
    #: state) that lives outside routes.yaml and isn't available in here.
    run_now: bool = False
    report_now: bool = False
    status_now: bool = False
    reset_now: bool = False


class Cursor:
    """Remembers the last handled message so a command never runs twice.

    Stored in the repo next to the price history, because the runner is a fresh
    container every time and has no other memory between runs.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.last_id = ""
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.last_id = str(loaded.get("last_id", ""))
            except (json.JSONDecodeError, OSError):
                # A lost cursor replays at most the ntfy cache window; refusing
                # to run would mean commands stop working entirely.
                self.last_id = ""

    @property
    def since(self) -> str:
        """What to pass to ntfy: everything after the last handled message.

        With no cursor yet, look back one poll interval rather than ``all``, so
        first use does not replay hours of history in one go.
        """
        return self.last_id or "30m"

    def record(self, message_id: str) -> None:
        if message_id:
            self.last_id = message_id

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"last_id": self.last_id}, indent=2) + "\n", encoding="utf-8"
        )


@dataclass
class Handled:
    """One message and what came of it."""

    command: Command
    outcome: CommandOutcome


def process(
    messages: list[dict],
    *,
    config_path: str | Path,
    secret: str,
    cursor: Cursor | None = None,
) -> list[Handled]:
    """Run every command in ``messages``, oldest first.

    The cursor advances past every message seen, including ones that failed:
    a command that cannot be applied will not succeed on the next poll either,
    and retrying it forever would report the same error every 30 minutes.
    """
    handled: list[Handled] = []

    for event in messages:
        message_id = str(event.get("id", ""))
        command = parse(event.get("message", ""), secret=secret)
        if cursor:
            cursor.record(message_id)
        if command is None:
            continue

        command.message_id = message_id
        try:
            outcome = apply(command, config_path)
        except CommandError as exc:
            outcome = CommandOutcome(False, f"✗ {exc}")
        except Exception as exc:  # noqa: BLE001 - never let one bad message stop the rest
            outcome = CommandOutcome(False, f"✗ 處理 /{command.verb} 時出錯：{type(exc).__name__}: {exc}")
        handled.append(Handled(command, outcome))

    return handled


# ---------------------------------------------------------------- parsing


def parse(text: str, *, secret: str) -> Command | None:
    """Parse one message into a :class:`Command`.

    Returns ``None`` for anything that is not addressed to us -- a message with
    the wrong secret, or no command at all. Silence is deliberate: a topic that
    answers "wrong password" tells a guesser they found a live channel.
    """
    if not secret:
        # Fail closed. Without a secret the topic name is the only thing
        # standing between a stranger and your config.
        return None

    text = (text or "").strip()
    if not text.startswith(secret):
        return None

    body = text[len(secret) :].strip()
    if not body.startswith("/"):
        return None

    parts = body.split()
    verb = parts[0][1:].lower()
    rest = parts[1:]

    target = None
    args = []
    for part in rest:
        if part.startswith("@") and len(part) > 1:
            target = part[1:]
        else:
            args.append(part)

    return Command(verb=verb, args=tuple(args), target=target, raw=body)


def _as_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"{value} 不是 YYYY-MM-DD 格式的日期") from None


def _as_positive_int(value: str, label: str) -> int:
    try:
        number = int(value.replace(",", ""))
    except ValueError:
        raise CommandError(f"{label} 要是數字，收到 {value}") from None
    if number <= 0:
        raise CommandError(f"{label} 要大於 0，收到 {number}")
    return number


# ---------------------------------------------------------------- YAML editing


def _yaml() -> YAML:
    editor = YAML()
    editor.preserve_quotes = True
    # routes.yaml is hand-written, commented, and meant to stay hand-editable.
    # Match its layout exactly so a command-driven edit does not reformat the
    # whole file and bury the real change in noise.
    editor.indent(mapping=2, sequence=4, offset=2)
    editor.width = 4096
    return editor


def _flow_seq(items) -> CommentedSeq:
    """A list that dumps inline as ``[SYD, OOL]`` rather than as block items."""
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def _pick_route(document, target: str | None):
    """Find the route a command applies to.

    Defaults to the first non-multi route: multi-city legs have fixed dates and
    airports, so "add a destination" means nothing there.
    """
    routes = document.get("routes") or []
    if not routes:
        raise CommandError("routes.yaml 裡沒有任何路線")

    if target:
        for route in routes:
            if str(route.get("name", "")) == target:
                return route
        names = "、".join(str(r.get("name", "?")) for r in routes)
        raise CommandError(f"找不到路線 {target}，目前有：{names}")

    for route in routes:
        if route.get("trip") != "multi":
            return route
    raise CommandError("只有多段行程路線，請用 @名稱 指定要改哪一條")


def describe_routes(config) -> str:
    lines = []
    for route in config.routes:
        if route.trip == "multi":
            legs = " → ".join(f"{o}>{d} {dt}" for o, d, dt in route.legs)
            lines.append(f"[{route.name}] 多段：{legs}")
            continue

        origins = ",".join(route.origins)
        destinations = ",".join(route.destinations)
        spans = []
        for window in route.windows:
            first, last = window.departs[0], window.departs[-1]
            span = first.isoformat() if first == last else f"{first}~{last}"
            spans.append(f"{span} 待{window.nights}晚" if window.nights is not None else span)
        thresholds = []
        if route.alert_below:
            thresholds.append(f"低於 {route.alert_below:,}")
        if route.alert_drop_pct:
            thresholds.append(f"跌 {route.alert_drop_pct:g}%")

        lines.append(
            f"[{route.name}] {origins} → {destinations}\n"
            f"  {'；'.join(spans)}\n"
            f"  通知：{' 或 '.join(thresholds) or '未設定'}"
            f"  轉機上限：{route.options.max_stops if route.options.max_stops is not None else '不限'}"
        )
    return "\n".join(lines)


def _validate_and_write(document, path: Path) -> str:
    """Write only if the edited config still parses and fits the query budget.

    Rendering to text and re-reading it through the normal loader means a
    command cannot introduce anything the scheduled run would choke on -- and
    a mistyped date range cannot quietly turn into 900 queries at Google.
    """
    buffer = io.StringIO()
    _yaml().dump(document, buffer)
    rendered = buffer.getvalue()

    scratch = path.with_suffix(path.suffix + ".check")
    try:
        scratch.write_text(rendered, encoding="utf-8")
        try:
            config = load_config(scratch)
            specs = expand_all(config)
        except TooManyQueries as exc:
            raise CommandError(f"改了會變成 {exc.wanted} 次查詢，超過上限 {exc.limit}，沒有套用") from None
        except ConfigError as exc:
            raise CommandError(f"改完的設定檔不合法，沒有套用：{exc}") from None
    finally:
        scratch.unlink(missing_ok=True)

    path.write_text(rendered, encoding="utf-8")
    return f"共 {len(specs)} 次查詢"


# ---------------------------------------------------------------- applying


def apply(command: Command, config_path: str | Path) -> CommandOutcome:
    """Execute one command, editing ``routes.yaml`` in place when it changes something."""
    path = Path(config_path)

    if command.verb in ("help", "?"):
        return CommandOutcome(True, HELP_TEXT)

    if command.verb == "run":
        return CommandOutcome(True, "好，馬上查一輪。", run_now=True)

    if command.verb == "report":
        return CommandOutcome(True, "", report_now=True)

    if command.verb == "status":
        return CommandOutcome(True, "", status_now=True)

    if command.verb == "reset":
        return CommandOutcome(True, "", reset_now=True)

    if command.verb == "routes":
        try:
            return CommandOutcome(True, describe_routes(load_config(path)))
        except ConfigError as exc:
            raise CommandError(f"讀不到設定檔：{exc}") from None

    editor = _yaml()
    try:
        document = editor.load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CommandError(f"讀不到 {path}：{exc}") from None

    if command.verb in ("newroute", "addroute"):
        # Operates on the whole routes list, not one picked route -- there is
        # no existing route to target yet.
        summary = _add_route(document, command.args)
        budget = _validate_and_write(document, path)
        return CommandOutcome(True, f"{summary}（{budget}）", changed=True)

    if command.verb in ("delroute", "rmroute"):
        summary = _remove_route(document, command.args)
        budget = _validate_and_write(document, path)
        return CommandOutcome(True, f"{summary}（{budget}）", changed=True)

    route = _pick_route(document, command.target)
    name = str(route.get("name", "?"))

    if command.verb == "rename":
        # Needs every route's name to reject a collision, which a per-route
        # edit function in _EDITS never sees -- handled here instead.
        if len(command.args) != 1:
            raise CommandError("用法：/rename 新名稱，例：/rename 台北-布里斯本")
        new_name = command.args[0]
        if any(str(r.get("name", "")) == new_name for r in document.get("routes") or []):
            raise CommandError(f"已經有路線叫 {new_name} 了")
        route["name"] = new_name
        budget = _validate_and_write(document, path)
        return CommandOutcome(True, f"[{name}] 改名為 {new_name}（{budget}）", changed=True)

    summary = _EDITS[command.verb](route, command.args) if command.verb in _EDITS else None
    if summary is None:
        raise CommandError(f"不認得的指令 /{command.verb}，輸入 /help 看可用指令")

    budget = _validate_and_write(document, path)
    return CommandOutcome(True, f"[{name}] {summary}（{budget}）", changed=True)


def _edit_scan(route, args) -> str:
    # Dates go in as date objects, not strings: YAML then writes them bare
    # (2027-03-01) instead of quoted, matching how a human would type them.
    if len(args) == 3:
        start, end, nights = _as_date(args[0]), _as_date(args[1]), _as_positive_int(args[2], "晚數")
        if end < start:
            raise CommandError(f"迄日 {end} 早於起日 {start}")
        fields = {"depart_range": _flow_seq([start, end]), "nights": nights}
        drop = "depart"
        label = f"掃描區間改為 {start}~{end}，待 {nights} 晚"
    elif len(args) == 2:
        depart, nights = _as_date(args[0]), _as_positive_int(args[1], "晚數")
        fields = {"depart": depart, "nights": nights}
        drop = "depart_range"
        label = f"掃描日期改為 {depart}，待 {nights} 晚"
    else:
        raise CommandError("用法：/scan 起日 迄日 晚數，或 /scan 出發日 晚數")

    windows = route.get("windows")
    if isinstance(windows, list) and len(windows) == 1 and isinstance(windows[0], dict):
        # Edit the existing window in place so its inline comments survive.
        window = windows[0]
        window.pop(drop, None)
        window.update(fields)
    else:
        # Several windows collapse into one: the command sets a single span.
        route["windows"] = [fields]

    return label


def _edit_nights(route, args) -> str:
    if len(args) != 1:
        raise CommandError("用法：/nights 晚數")
    nights = _as_positive_int(args[0], "晚數")
    windows = route.get("windows")
    if not windows:
        raise CommandError("這條路線還沒有設定日期，請先用 /scan")
    for window in windows:
        window["nights"] = nights
    return f"待幾晚改為 {nights}"


def _destination_list(route) -> list:
    """The route's ``to`` as a mutable list node.

    A single code (``to: SYD``) is promoted to a one-item flow list so the
    caller can just append; anything already a list is returned as the live
    node, so edits keep its original ``[SYD, OOL]`` inline formatting.
    """
    current = route.get("to")
    if current is None:
        raise CommandError("這條路線沒有目的地欄位")
    if isinstance(current, str):
        promoted = _flow_seq([current])
        route["to"] = promoted
        return promoted
    return current


def _edit_add(route, args) -> str:
    if len(args) != 1:
        raise CommandError("用法：/add 機場代碼，例：/add BNE")
    code = args[0].upper()
    if not AIRPORT_CODE.match(code):
        raise CommandError(f"{args[0]} 不像機場代碼（要三個英文字母，例如 BNE）")

    destinations = _destination_list(route)
    if code in destinations:
        raise CommandError(f"{code} 已經在追了")

    destinations.append(code)
    return f"加入目的地 {code}，現在追 {'、'.join(destinations)}"


def _edit_remove(route, args) -> str:
    if len(args) != 1:
        raise CommandError("用法：/rm 機場代碼，例：/rm OOL")
    code = args[0].upper()

    destinations = _destination_list(route)
    if code not in destinations:
        raise CommandError(f"{code} 不在追蹤清單裡，目前是 {'、'.join(destinations)}")
    if len(destinations) == 1:
        raise CommandError("這是最後一個目的地，移掉就沒東西可追了。整個換掉的話用 /to")

    destinations.remove(code)
    return f"移除目的地 {code}，現在追 {'、'.join(destinations)}"


def _parse_codes(args) -> list[str]:
    if not args:
        raise CommandError("至少要給一個機場代碼")
    codes = []
    for raw in args:
        code = raw.upper()
        if not AIRPORT_CODE.match(code):
            raise CommandError(f"{raw} 不像機場代碼（要三個英文字母，例如 BNE）")
        codes.append(code)
    return codes


def _replace_codes(route, field: str, args) -> str:
    """Replace the whole ``from``/``to`` list in one shot.

    /add and /rm only nudge an existing list, and /rm refuses to empty it --
    so swapping a single-destination route's one and only code (the exact
    situation Google Flights returning no results for it forces) needs its own
    command rather than a remove-then-add that can never complete.
    """
    which = "出發地" if field == "from" else "目的地"
    if route.get(field) is None:
        raise CommandError(f"這條路線沒有{which}欄位（多段行程請直接改 legs）")

    codes = _parse_codes(args)
    route[field] = codes[0] if len(codes) == 1 else _flow_seq(codes)
    return f"{which}整個換成 {'、'.join(codes)}"


def _edit_to(route, args) -> str:
    return _replace_codes(route, "to", args)


def _edit_from(route, args) -> str:
    return _replace_codes(route, "from", args)


def _add_route(document, args) -> str:
    """Create a whole new round-trip route, appended to ``routes``.

    Deliberately round-trip only: a one-way or multi-city route needs shapes
    (no ``nights``, or a ``legs`` list) that don't fit one command line without
    getting as fiddly as just editing routes.yaml directly.
    """
    if len(args) not in (5, 6):
        raise CommandError(
            "用法：/newroute 名稱 出發地 目的地 出發日 晚數 [門檻金額]，"
            "例：/newroute 台北-福岡 TPE FUK 2027-06-05 7 20000"
        )
    name, origin_raw, destination_raw, depart_raw, nights_raw, *rest = args

    routes = document.get("routes")
    if routes is None:
        raise CommandError("routes.yaml 裡沒有 routes 欄位")
    if any(str(r.get("name", "")) == name for r in routes):
        raise CommandError(f"已經有路線叫 {name} 了，用 /scan /to 之類的指令去改它，或先 /delroute 舊的")

    origin, destination = origin_raw.upper(), destination_raw.upper()
    if not AIRPORT_CODE.match(origin):
        raise CommandError(f"{origin_raw} 不像機場代碼（要三個英文字母，例如 TPE）")
    if not AIRPORT_CODE.match(destination):
        raise CommandError(f"{destination_raw} 不像機場代碼（要三個英文字母，例如 FUK）")

    depart = _as_date(depart_raw)
    nights = _as_positive_int(nights_raw, "晚數")
    alert_below = _as_positive_int(rest[0], "門檻金額") if rest else None

    entry = {
        "name": name,
        "from": origin,
        "to": destination,
        "trip": "round",
        "windows": [{"depart": depart, "nights": nights}],
    }
    if alert_below is not None:
        entry["alert_below"] = alert_below
    routes.append(entry)

    label = f"新增路線 [{name}] {origin} → {destination}，{depart} 出發、待 {nights} 晚"
    if alert_below is not None:
        label += f"，門檻 {alert_below:,}"
    return label


def _remove_route(document, args) -> str:
    if len(args) != 1:
        raise CommandError("用法：/delroute 名稱，例：/delroute 台北-福岡")
    name = args[0]

    routes = document.get("routes")
    if not routes:
        raise CommandError("routes.yaml 裡沒有任何路線")
    if len(routes) == 1:
        raise CommandError("這是最後一條路線，刪掉就沒東西可追了。要換地方用 /to，不是 /delroute")

    for index, route in enumerate(routes):
        if str(route.get("name", "")) == name:
            del routes[index]
            return f"刪除路線 [{name}]"

    names = "、".join(str(r.get("name", "?")) for r in routes)
    raise CommandError(f"找不到路線 {name}，目前有：{names}")


def _edit_price(route, args) -> str:
    if len(args) != 1:
        raise CommandError("用法：/price 金額，例：/price 24000")
    amount = _as_positive_int(args[0], "金額")
    route["alert_below"] = amount
    return f"通知門檻改為 {amount:,}"


def _edit_drop(route, args) -> str:
    if len(args) != 1:
        raise CommandError("用法：/drop 百分比，例：/drop 12")
    pct = _as_positive_int(args[0], "百分比")
    if pct >= 100:
        raise CommandError("百分比要小於 100")
    route["alert_drop_pct"] = pct
    return f"跌價通知門檻改為 {pct}%"


def _edit_stops(route, args) -> str:
    if len(args) != 1:
        raise CommandError("用法：/stops 轉機次數上限，例：/stops 1")
    try:
        stops = int(args[0])
    except ValueError:
        raise CommandError(f"轉機次數要是數字，收到 {args[0]}") from None
    if stops < 0:
        raise CommandError("轉機次數不能是負數")
    route["max_stops"] = stops
    return f"轉機次數上限改為 {stops}"


_EDITS = {
    "scan": _edit_scan,
    "time": _edit_scan,  # alias: easier to remember than "scan" for "change the date"
    "nights": _edit_nights,
    "add": _edit_add,
    "rm": _edit_remove,
    "remove": _edit_remove,
    "to": _edit_to,
    "location": _edit_to,  # alias: easier to remember than "to" for "change the destination"
    "from": _edit_from,
    "price": _edit_price,
    "below": _edit_price,
    "drop": _edit_drop,
    "stops": _edit_stops,
}
