from __future__ import annotations

from pathlib import Path


def test_domain_does_not_own_external_market_payload_parser() -> None:
    domain_files = [path.name for path in Path("src/polymarket_trader/domain").glob("*.py")]
    assert "classifier.py" not in domain_files
