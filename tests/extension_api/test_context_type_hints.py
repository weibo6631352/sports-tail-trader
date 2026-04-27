from __future__ import annotations

from typing import get_type_hints

from polymarket_trader.extension_api.context import ExtensionContext
from polymarket_trader.extension_api.decisions import EntryCandidate, MarketTokenView


def test_strategy_context_type_hints_resolve_runtime_annotations() -> None:
    hints = get_type_hints(ExtensionContext)

    assert hints["market_token_views"] == tuple[MarketTokenView, ...]
    assert hints["entry_candidates"] == tuple[EntryCandidate, ...]
