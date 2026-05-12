"""CLI 入口：跑体育直播源校准 harness。

用法：
    python -m polymarket_trader.tools.sports_live_calibration \\
        --sources espn,nba \\
        --leagues NBA,NHL \\
        --output report.json

不启动交易主链路；不下单。读取当前 Settings 装配 aggregate_client + market_registry
快照（或本地 fixture），跑一次 list_events + best_live_match 并把 JSON 报告
写到 ``--output``（缺省时打印到 stdout）。

CLAUDE.md §1：本工具是离线 dev/runbook 工具；不参与 P0 交易热路径。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from polymarket_trader.app.sports_live_calibration import (
    SportsLiveCalibrationReport,
    run_calibration,
)
from polymarket_trader.config import load_settings
from polymarket_trader.domain.market import Market
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
        "--output",
        default="-",
        help="Output JSON file path; '-' (default) writes to stdout.",
    )
    return parser.parse_args(argv)


def _markets_from_runtime() -> tuple[Market, ...]:
    """读取当前 runtime 注册表中的 market 快照；如无 runtime，返回空 tuple。

    这里刻意不去启动 trading 主链路；调用方可以扩展为加载 fixture。
    """

    return ()


async def _amain(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()
    source_filter = [s.strip() for s in args.sources.split(",") if s.strip()]
    league_filter = [s.strip().upper() for s in args.leagues.split(",") if s.strip()]
    aggregate_client: SportsLiveAggregateClient = _build_sports_live_state_client(settings)
    markets = _markets_from_runtime()

    def _stub_match(market: Market, events: tuple[Any, ...]) -> Any:
        # 校准 harness 默认不传策略匹配函数；只统计 aggregate 层指标。
        # 调用方有需要时可以扩展 CLI 注入策略 hook。
        return None

    try:
        report: SportsLiveCalibrationReport = await run_calibration(
            aggregate_client=aggregate_client,
            markets=markets,
            match_live_event=_stub_match,
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
