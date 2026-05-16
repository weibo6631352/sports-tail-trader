"""CLI 入口：跑体育直播源校准 harness。

两种用法：

1. **Aggregate-only**：只校准直播源拉取/融合/去重链路，不需要 market 数据。
   ::

       python -m polymarket_trader.tools.sports_live_calibration \\
           --sources espn,nba --leagues NBA,NHL --output report.json

2. **With markets**：通过 ``--markets-fixture`` 传入本地 JSON 样本，CLI 用策略
   ``best_live_match`` 计算 per-market 匹配率与 unmatched 原因。
   ::

       python -m polymarket_trader.tools.sports_live_calibration \\
           --markets-fixture tests/fixtures/sports_live/markets_sample.json \\
           --output report.json

fixture JSON schema（最小字段；其余 Market 字段用默认值即可）::

    [
      {
        "condition_id": "0xabc...",
        "market_slug": "nba-magic-pistons-2026-05-12",
        "market_question": "Will the Magic beat the Pistons?",
        "event_title": "Magic @ Pistons",
        "event_slug": "magic-pistons-2026-05-12",
        "category": "Sports",
        "tags": ["NBA", "Basketball"],
        "outcomes": [
          {"token_id": "h", "outcome": "Orlando Magic"},
          {"token_id": "a", "outcome": "Detroit Pistons"}
        ]
      }
    ]

不启动交易主链路；不下单。读取当前 Settings 装配 aggregate_client，跑一次或多次
``list_events`` + 可选 ``best_live_match`` 并把 JSON 报告写到 ``--output``。

CLAUDE.md §1：本工具是离线 dev/runbook 工具；不参与 P0 交易热路径。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from polymarket_trader.app.extension_host import load_extension
from polymarket_trader.app.sports_live_calibration import (
    SportsLiveCalibrationReport,
    run_calibration,
)
from polymarket_trader.config import load_settings
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.infra.sports.aggregate_client import SportsLiveAggregateClient
from polymarket_trader.main import _build_sports_live_state_client


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sports_live_calibration",
        description=(
            "Probe configured sports live sources and report per-source / per-league "
            "coverage and silent_gap warnings without touching the trading path."
        ),
    )
    parser.add_argument(
        "--sources",
        default="",
        help="Comma-separated source codes to include in the report (e.g. espn,nba).",
    )
    parser.add_argument(
        "--leagues",
        default="",
        help="Comma-separated league codes to filter in the report (e.g. NBA,NHL).",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Number of aggregate fetches to run (default 1).",
    )
    parser.add_argument(
        "--silent-gap-threshold",
        type=float,
        default=0.5,
        help="Match-rate floor below which a silent_gap warning is raised (default 0.5).",
    )
    parser.add_argument(
        "--markets-fixture",
        default=None,
        help=(
            "Path to a JSON file of market fixtures (see module docstring). "
            "Omitted = aggregate-only mode (no per-market matching)."
        ),
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Output JSON file path; '-' (default) writes to stdout.",
    )
    return parser.parse_args(argv)


def load_markets_from_fixture(path: str | Path) -> tuple[Market, ...]:
    """从 JSON fixture 加载 minimal Market 列表。

    只读 calibration 必需字段（condition_id / slug / question / event / category /
    tags / outcomes）；其他字段用 Market dataclass 默认值。Schema 见模块 docstring。
    """

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(
            f"markets fixture {path!s} must be a JSON list of market objects"
        )
    markets: list[Market] = []
    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ValueError(f"markets fixture entry #{index} must be an object")
        outcomes_raw = raw.get("outcomes") or ()
        outcomes = tuple(
            MarketOutcome(token_id=str(o["token_id"]), outcome=str(o["outcome"]))
            for o in outcomes_raw
            if isinstance(o, dict) and "token_id" in o and "outcome" in o
        )
        markets.append(
            Market(
                condition_id=str(raw["condition_id"]),
                market_slug=str(raw["market_slug"]),
                market_name=raw.get("market_name"),
                market_question=raw.get("market_question"),
                event_id=raw.get("event_id"),
                event_title=raw.get("event_title"),
                event_slug=raw.get("event_slug"),
                category=raw.get("category"),
                tags=tuple(raw.get("tags") or ()),
                outcomes=outcomes,
                trading_status=TradingStatus(raw.get("trading_status", "eligible")),
            )
        )
    return tuple(markets)


async def _amain(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()
    source_filter = [s.strip() for s in args.sources.split(",") if s.strip()]
    league_filter = [s.strip().upper() for s in args.leagues.split(",") if s.strip()]
    aggregate_client: SportsLiveAggregateClient = _build_sports_live_state_client(settings)

    match_fn: Any = None
    if args.markets_fixture:
        markets: tuple[Market, ...] = load_markets_from_fixture(args.markets_fixture)
        if settings.extension_module:
            extension = load_extension(module_path=settings.extension_module)
            live_state_hooks = extension.live_state_hooks
            if live_state_hooks is not None:
                match_fn = live_state_hooks.match_live_state
            else:
                print(
                    "WARNING: extension has no live_state_hooks — running aggregate-only mode "
                    "(per-market match rates will not be computed)",
                    file=sys.stderr,
                )
        else:
            print(
                "WARNING: extension_module not configured — running aggregate-only mode "
                "(per-market match rates will not be computed)",
                file=sys.stderr,
            )
    else:
        markets = ()

    try:
        report: SportsLiveCalibrationReport = await run_calibration(
            aggregate_client=aggregate_client,
            markets=markets,
            match_live_event=match_fn,
            rounds=max(1, int(args.rounds)),
            silent_gap_threshold=float(args.silent_gap_threshold),
            league_filter=league_filter or None,
            source_filter=source_filter or None,
        )
    finally:
        await aggregate_client.aclose()

    payload = json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
    if args.output == "-":
        sys.stdout.write(payload + "\n")
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload)
    # 非零退出码用于 CI：silent_gap 触发或 failures 非空时返回 1，便于运维报警。
    if report.silent_gaps or report.failures:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
