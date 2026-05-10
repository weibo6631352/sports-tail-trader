"""``python -m polymarket_trader.tools.replay_decisions`` 决策回放 CLI。

工作流：
    1. ``dump-recorder``    把当前进程的 InMemoryDecisionRecorder snapshot 写到 JSONL
    2. ``tail``             读 JSONL 按市场 / hook 过滤展示
    3. ``replay``           基于 JSONL 调用指定策略 replay 函数，输出 diff 报告

本 CLI 是离线工具，不进 P0 主链路；它读取的 JSONL 是 framework
``dump_records_to_jsonl`` 输出的格式。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from polymarket_trader.app.replay_harness import (
    ReplayDiffKind,
    replay_records,
)
from polymarket_trader.extension_api.recorder import (
    DecisionRecord,
    dump_records_to_jsonl,
)


def _iter_records(path: Path) -> Iterator[DecisionRecord]:
    with path.open("r", encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                print(f"warning: line {line_no} not valid JSON: {exc}", file=sys.stderr)
                continue
            yield _record_from_payload(payload)


def _record_from_payload(payload: dict[str, Any]) -> DecisionRecord:
    recorded_at_text = payload.get("recorded_at")
    recorded_at = (
        datetime.fromisoformat(recorded_at_text)
        if isinstance(recorded_at_text, str)
        else datetime.fromtimestamp(0)
    )
    return DecisionRecord(
        hook_name=str(payload.get("hook_name", "")),
        trace_id=str(payload.get("trace_id", "")),
        recorded_at=recorded_at,
        context_payload=payload.get("context_payload", {}) or {},
        decision_payload=payload.get("decision_payload", {}) or {},
        condition_id=payload.get("condition_id"),
        token_id=payload.get("token_id"),
        market_slug=payload.get("market_slug"),
        extras=payload.get("extras", {}) or {},
    )


def _filter(records: Iterator[DecisionRecord], *, market: str | None, hook: str | None) -> Iterator[DecisionRecord]:
    for record in records:
        if market and (record.condition_id != market and record.market_slug != market):
            continue
        if hook and record.hook_name != hook:
            continue
        yield record


def _cmd_tail(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    seen = 0
    for record in _filter(_iter_records(path), market=args.market, hook=args.hook):
        seen += 1
        action = record.decision_payload.get("action") if isinstance(record.decision_payload, dict) else None
        reason = record.decision_payload.get("reason") if isinstance(record.decision_payload, dict) else None
        print(
            f"{record.recorded_at.isoformat()}  {record.hook_name:<18}  "
            f"{(record.condition_id or record.market_slug or '-')[:32]:<32}  "
            f"action={action}  reason={reason}"
        )
        if args.limit and seen >= args.limit:
            break
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    module_name, _, attr_name = args.replay_callable.partition(":")
    if not module_name or not attr_name:
        print("--replay-callable must be 'module.path:function_name'", file=sys.stderr)
        return 2
    import importlib

    module = importlib.import_module(module_name)
    replay_decision = getattr(module, attr_name)
    records = tuple(_filter(_iter_records(path), market=args.market, hook=args.hook))
    report = replay_records(records, replay_decision=replay_decision)
    counts = report.by_kind
    print(f"replayed {len(records)} records; changed={report.changed_count}")
    for kind in ReplayDiffKind:
        print(f"  {kind.value:<18} {counts.get(kind, 0)}")
    if args.show_changes:
        for diff in report.diffs:
            if diff.kind == ReplayDiffKind.UNCHANGED:
                continue
            print(f"  trace={diff.record.trace_id} kind={diff.kind.value} detail={diff.detail}")
    return 0


def _cmd_dump_recorder(args: argparse.Namespace) -> int:
    """从 admin /decisions/dump endpoint 抓 records，落 JSONL。

    需要 ``--admin-url``（如 http://127.0.0.1:8000）；endpoint 返回 JSON
    ``{"records": [...]}``，每个元素结构与 ``dump_records_to_jsonl`` 输入一致。
    """

    import urllib.error
    import urllib.request

    url = args.admin_url.rstrip("/") + "/admin/decisions/dump"
    try:
        with urllib.request.urlopen(url, timeout=args.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"failed to reach admin endpoint: {exc}", file=sys.stderr)
        return 2
    raw_records = payload.get("records", [])
    if not isinstance(raw_records, list):
        print("admin response missing 'records' list", file=sys.stderr)
        return 2
    records = [_record_from_payload(item) for item in raw_records if isinstance(item, dict)]
    written = dump_records_to_jsonl(records, Path(args.output))
    print(f"wrote {written} records to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay framework-recorded decisions offline.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    dump = sub.add_parser("dump-recorder", help="Pull records from a running admin endpoint into a JSONL file.")
    dump.add_argument("--admin-url", required=True, help="admin base URL, e.g. http://127.0.0.1:8000")
    dump.add_argument("--output", required=True, help="JSONL output path")
    dump.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds")
    dump.set_defaults(func=_cmd_dump_recorder)

    tail = sub.add_parser("tail", help="Print recorded decisions in human-readable form.")
    tail.add_argument("path", help="JSONL file produced by dump_records_to_jsonl")
    tail.add_argument("--market", help="filter by condition_id or market_slug")
    tail.add_argument("--hook", help="filter by hook_name (e.g. decide_entry)")
    tail.add_argument("--limit", type=int, default=0, help="max records to print (0 = unlimited)")
    tail.set_defaults(func=_cmd_tail)

    replay = sub.add_parser("replay", help="Re-run records through a Python replay callable.")
    replay.add_argument("path", help="JSONL file produced by dump_records_to_jsonl")
    replay.add_argument(
        "--replay-callable",
        required=True,
        help="callable spec 'module.path:function_name' returning new decision_payload dict",
    )
    replay.add_argument("--market", help="filter by condition_id or market_slug")
    replay.add_argument("--hook", help="filter by hook_name (e.g. decide_entry)")
    replay.add_argument("--show-changes", action="store_true", help="print every changed diff")
    replay.set_defaults(func=_cmd_replay)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
