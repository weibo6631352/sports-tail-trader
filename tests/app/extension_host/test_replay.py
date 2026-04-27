from __future__ import annotations

from pathlib import Path

from polymarket_trader.app.extension_host import run_entry_replay


DEMO_EXTENSION_MODULE = "tests.helpers.demo_extension"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "entry_replay.json"


def test_run_entry_replay_uses_explicit_extension_fixture() -> None:
    payload = run_entry_replay(
        str(FIXTURE_PATH),
        extension_module=DEMO_EXTENSION_MODULE,
    )

    assert payload["extension"]["name"] == "demo"
    assert payload["extension"]["extension_module"] == DEMO_EXTENSION_MODULE
    assert payload["plan"]["ready_to_trade"] is True
    assert payload["plan"]["intent"]["amount_usdc"] == "50"


def test_run_entry_replay_requires_extension_module() -> None:
    try:
        run_entry_replay(str(FIXTURE_PATH))
    except ValueError as exc:
        assert str(exc) == "extension_module is required for entry replay"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected ValueError")
