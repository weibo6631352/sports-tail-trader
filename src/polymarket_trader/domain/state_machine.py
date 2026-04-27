from __future__ import annotations

from enum import StrEnum


class MarketLifecycle(StrEnum):
    DISCOVERED = "discovered"
    CLASSIFIED = "classified"
    WATCHING_ORDERBOOK = "watching_orderbook"
    ENTRY_READY = "entry_ready"
    ENTRY_SUBMITTING = "entry_submitting"
    ENTRY_REJECTED = "entry_rejected"
    POSITION_OPEN = "position_open"
    FOLLOW_UP_ORDER_OPEN = "follow_up_order_open"
    PAUSED = "paused"
    CLOSED = "closed"
