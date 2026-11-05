#!/usr/bin/env python3
"""Live draw probability for every tile on the MING grid.

The stats page and the mine grid read the same feed at core.ming.family.
Published rules (docs/the-round): each of the 25 tiles has an equal chance
of being drawn, and the amount mined on a tile does not change that chance.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from fractions import Fraction
from typing import Any
from zoneinfo import ZoneInfo

API = "https://core.ming.family"
EVENTS_PATH = "/v1/events"
ROUND_PATH = "/v1/round"
STATS_PATH = "/v1/stats"
SHANGHAI = ZoneInfo("Asia/Shanghai")
USER_AGENT = "ming-grid-probability/1.0"
# Same fallback the site uses when the event stream is down.
DEFAULT_POLL_SECONDS = 5
COLUMNS = 5
WEI = 10**18


class FeedError(RuntimeError):
    pass


def cell_probabilities(tile_count: int, drawn: int | None = None) -> list[Fraction]:
    """Probability that each tile is the one drawn this round.

    While the draw is unpublished, every tile is equally likely.
    Once this round's payload names the drawn tile, that cell is 1 and the rest are 0.
    """
    if tile_count <= 0:
        raise ValueError("tile_count must be positive")
    if drawn is None:
        return [Fraction(1, tile_count) for _ in range(tile_count)]
    if not isinstance(drawn, int) or isinstance(drawn, bool) or not 0 <= drawn < tile_count:
        raise ValueError(f"drawn tile {drawn} is outside 0..{tile_count - 1}")
    probs = [Fraction(0) for _ in range(tile_count)]
    probs[drawn] = Fraction(1)
    return probs


def _as_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def current_drawn(
    round_payload: dict[str, Any] | None,
    last_settled: dict[str, Any] | None = None,
) -> int | None:
    """Drawn index once this round has published it.

    The open-round payload usually has no winner yet. A settlement for the
    same round id is the publication, until the feed moves on to the next round.
    """
    if not round_payload:
        return None
    direct = _as_index(round_payload.get("drawn"))
    if direct is not None:
        return direct
    if last_settled and last_settled.get("round") == round_payload.get("id"):
        return _as_index(last_settled.get("drawn"))
    return None


def apply_entry(round_payload: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Fold one deploy into the open round, matching the site's feed client."""
    if entry.get("round") != round_payload.get("id"):
        return round_payload
    tiles = entry.get("tiles") or []
    per_tile = int(entry.get("perTile") or 0)
    added = int(entry.get("total") or 0)
    totals = [int(value) for value in round_payload.get("tileTotals") or []]
    for index in tiles:
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(totals):
            totals[index] += per_tile
    updated = dict(round_payload)
    updated["tileTotals"] = [str(value) for value in totals]
    updated["total"] = str(int(round_payload.get("total") or 0) + added)
    return updated


def snapshot_key(
    round_payload: dict[str, Any] | None,
    stats: dict[str, Any] | None,
    last_settled: dict[str, Any] | None = None,
) -> str:
    tiles = tuple((round_payload or {}).get("tileTotals") or [])
    drawn = current_drawn(round_payload, last_settled)
    stats = stats or {}
    return json.dumps(
        {
            "id": (round_payload or {}).get("id"),
            "phase": (round_payload or {}).get("phase"),
            "tiles": tiles,
            "total": (round_payload or {}).get("total"),
            "players": (round_payload or {}).get("players"),
            "drawn": drawn,
            "roundsSettled": stats.get("roundsSettled"),
            "roundsWithEntries": stats.get("roundsWithEntries"),
            "entriesAllTime": stats.get("entriesAllTime"),
            "minedAllTime": stats.get("minedAllTime"),
            "goldOdds": stats.get("goldOdds"),
        },
        sort_keys=True,
        default=str,
    )


def format_bnb(wei: int) -> str:
    whole, frac = divmod(abs(int(wei)), WEI)
    text = f"{whole}.{frac:018d}"
    text = text[: len(str(whole)) + 1 + 6].rstrip("0").rstrip(".")
    if int(wei) < 0:
        return "-" + text
    return text or "0"


def format_percent(prob: Fraction) -> str:
    # 1/25 is exactly 4%. Format from the rational so other N stay readable.
    return f"{float(prob * 100):.4f}%"


def render(
    round_payload: dict[str, Any] | None,
    stats: dict[str, Any] | None,
    *,
    source: str,
    last_settled: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %z")
    lines = [now, f"来源: {source}"]
    if not round_payload or "tileTotals" not in round_payload:
        lines.append("当前回合还没有格子数据。")
        return "\n".join(lines) + "\n"

    totals = [int(value) for value in round_payload["tileTotals"]]
    count = len(totals)
    drawn = current_drawn(round_payload, last_settled)
    if drawn is not None and not 0 <= drawn < count:
        drawn = None
    probs = cell_probabilities(count, drawn)
    phase = "已开出" if drawn is not None else round_payload.get("phase")
    lines.append(
        f"回合 {round_payload.get('id')}  阶段 {phase}  "
        f"本回合矿工 {round_payload.get('players')}  "
        f"本回合部署 {format_bnb(int(round_payload.get('total') or 0))} BNB"
    )
    if stats:
        lines.append(
            "已结算回合 {rounds}  有部署的回合 {with_entries}  累计入场 {entries}  "
            "金池条件赔率 1/{odds}".format(
                rounds=stats.get("roundsSettled"),
                with_entries=stats.get("roundsWithEntries"),
                entries=stats.get("entriesAllTime"),
                odds=stats.get("goldOdds"),
            )
        )
    if count != 25:
        lines.append(f"注意: 本回合格子数为 {count}，不是文档中的 25。概率按实况数组长度计算。")
    if drawn is None:
        lines.append(f"每一格被开出的概率 = 1/{count}。部署额不进入公式。")
    else:
        lines.append(f"本回合已公布开出第 {drawn + 1} 格，该格概率为 1，其余为 0。")

    lines.append("")
    lines.append("开出概率")
    lines.extend(_rows(count, lambda index: f"{index + 1:>2} {probs[index]} {format_percent(probs[index])}"))
    lines.append(f"概率合计 {sum(probs, Fraction(0))}")
    lines.append("")
    lines.append("各格已部署 BNB（只影响开出后的分成，不改变开出概率）")
    lines.extend(_rows(count, lambda index: f"{index + 1:>2} {format_bnb(totals[index])}"))
    if last_settled and last_settled.get("drawn") is not None and last_settled.get("round") != round_payload.get("id"):
        lines.append(
            f"上一结算回合 {last_settled.get('round')} 开出第 {int(last_settled['drawn']) + 1} 格"
        )
    return "\n".join(lines) + "\n"


def _rows(count: int, cell) -> list[str]:
    rows = []
    width = COLUMNS if count == 25 else COLUMNS
    for start in range(0, count, width):
        rows.append("  ".join(cell(index) for index in range(start, min(start + width, count))))
    return rows


def get_json(path: str, timeout: float = 20) -> Any:
    request = urllib.request.Request(
        API + path,
        headers={"accept": "application/json", "user-agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 429:
            retry = float(error.headers.get("retry-after") or 2)
            time.sleep(min(retry, 30))
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        raise FeedError(f"{path} -> HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise FeedError(f"{path} -> {error.reason}") from error


def fetch_snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
    return get_json(ROUND_PATH), get_json(STATS_PATH)


def iter_events(timeout: float = 70):
    request = urllib.request.Request(
        API + EVENTS_PATH,
        headers={"accept": "text/event-stream", "user-agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        event_name = "message"
        data: list[str] = []
        while True:
            raw = response.readline()
            if raw == b"":
                if data:
                    yield event_name, "\n".join(data)
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data:
                    yield event_name, "\n".join(data)
                event_name = "message"
                data = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())


def run_once() -> int:
    round_payload, stats = fetch_snapshot()
    sys.stdout.write(render(round_payload, stats, source=f"GET {API}{ROUND_PATH} + {STATS_PATH}"))
    return 0


def run_live(poll_seconds: float) -> int:
    round_payload: dict[str, Any] | None = None
    stats: dict[str, Any] | None = None
    last_settled: dict[str, Any] | None = None
    seen = ""
    backoff = 2.0

    def publish(source: str) -> None:
        nonlocal seen
        key = snapshot_key(round_payload, stats, last_settled)
        if key == seen:
            return
        seen = key
        sys.stdout.write(
            render(round_payload, stats, source=source, last_settled=last_settled)
        )
        sys.stdout.write("\n")
        sys.stdout.flush()

    try:
        round_payload, stats = fetch_snapshot()
        publish(f"GET {API}{ROUND_PATH} + {STATS_PATH}")
    except FeedError as error:
        print(f"首次拉取失败: {error}", file=sys.stderr)

    while True:
        try:
            for name, payload in iter_events():
                backoff = 2.0
                if name == "heartbeat":
                    continue
                try:
                    body = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if name == "round" and isinstance(body, dict):
                    round_payload = body
                elif name == "entry" and isinstance(body, dict) and round_payload:
                    round_payload = apply_entry(round_payload, body)
                elif name == "settled" and isinstance(body, dict):
                    last_settled = body
                elif name == "stats" and isinstance(body, dict):
                    stats = body
                else:
                    continue
                publish(f"SSE {API}{EVENTS_PATH} event={name}")
        except KeyboardInterrupt:
            return 0
        except (FeedError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            print(f"事件流中断 ({error})，改为每 {poll_seconds:g} 秒轮询。", file=sys.stderr)
        else:
            print("事件流已结束，准备重连。", file=sys.stderr)

        deadline = time.monotonic() + backoff
        while time.monotonic() < deadline:
            try:
                round_payload, stats = fetch_snapshot()
                publish(f"GET {API}{ROUND_PATH} + {STATS_PATH}（事件流中断后的轮询）")
            except FeedError as error:
                print(f"轮询失败: {error}", file=sys.stderr)
            except KeyboardInterrupt:
                return 0
            remaining = deadline - time.monotonic()
            step = min(poll_seconds, max(0.0, remaining))
            if step <= 0:
                break
            try:
                time.sleep(step)
            except KeyboardInterrupt:
                return 0
        backoff = min(backoff * 2, 30.0)


def self_check() -> int:
    probs = cell_probabilities(25)
    assert probs == [Fraction(1, 25)] * 25
    assert sum(probs) == 1
    posted = cell_probabilities(25, 10)
    assert posted[10] == 1 and posted.count(0) == 24 and sum(posted) == 1
    try:
        cell_probabilities(25, 25)
    except ValueError:
        pass
    else:
        raise AssertionError("out-of-range drawn tile was accepted")
    base = {
        "id": 7,
        "phase": "open",
        "tileTotals": ["10", "0"],
        "total": "10",
        "players": 1,
    }
    updated = apply_entry(base, {"round": 7, "tiles": [1], "perTile": "5", "total": "5", "player": "0xabc"})
    assert updated["tileTotals"] == ["10", "5"]
    assert updated["total"] == "15"
    ignored = apply_entry(base, {"round": 8, "tiles": [0], "perTile": "1", "total": "1"})
    assert ignored["total"] == "10"
    assert current_drawn({"id": 7, "phase": "open"}) is None
    assert current_drawn({"id": 7, "drawn": 0}) == 0
    assert current_drawn({"id": 7}, {"round": 7, "drawn": 3}) == 3
    assert current_drawn({"id": 8}, {"round": 7, "drawn": 3}) is None
    text = render(
        {"id": 7, "phase": "open", "tileTotals": ["0"] * 25, "total": "0", "players": 0},
        {"roundsSettled": 1, "roundsWithEntries": 1, "entriesAllTime": 1, "goldOdds": 888},
        source="self-check",
    )
    assert "1/25" in text and "概率合计 1" in text and "+08" in text
    settled = render(
        {"id": 7, "phase": "open", "tileTotals": ["1"] * 25, "total": "25", "players": 1},
        None,
        source="self-check",
        last_settled={"round": 7, "drawn": 0},
    )
    assert "开出第 1 格" in settled and "概率合计 1" in settled
    print("self-check ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 MING 网格的实况数据重算每一格的开出概率。")
    parser.add_argument("--once", action="store_true", help="拉取一次并打印，然后退出")
    parser.add_argument("--self-check", action="store_true", help="只跑离线公式检查")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="事件流断开时的轮询间隔，默认 5 秒，与页面客户端一致",
    )
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds 必须大于 0")
    if args.once:
        return run_once()
    return run_live(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except FeedError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
