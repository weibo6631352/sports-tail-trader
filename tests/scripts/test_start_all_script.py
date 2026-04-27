from __future__ import annotations

import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
START_ALL = ROOT_DIR / "start_all.sh"


def test_start_all_stops_existing_services_before_backend_bootstrap(tmp_path: Path) -> None:
    script = START_ALL.read_text(encoding="utf-8")
    assert script.rstrip().endswith('main "$@"')

    harness = tmp_path / "start_all_harness.sh"
    harness.write_text(
        script.rsplit('main "$@"', 1)[0]
        + """
ensure_runtime_python() { printf 'ensure_runtime_python\\n'; }
load_env() { printf 'load_env\\n'; }
stop_existing_services() { printf 'stop_existing_services\\n'; }
ensure_backend() { printf 'ensure_backend\\n'; }
ensure_frontend() { printf 'ensure_frontend\\n'; }
open_browser() { printf 'open_browser\\n'; return 0; }
log() { printf 'log:%s\\n' "$*"; }
main "$@"
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(harness)],
        cwd=ROOT_DIR,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    calls = [line for line in result.stdout.splitlines() if not line.startswith("log:")]
    assert calls[:4] == [
        "ensure_runtime_python",
        "load_env",
        "stop_existing_services",
        "ensure_backend",
    ]


def test_start_all_does_not_write_frontend_or_backend_process_logs() -> None:
    script = START_ALL.read_text(encoding="utf-8")

    assert "BACKEND_LOG=" not in script
    assert "FRONTEND_LOG=" not in script
    assert '>>"$log_file" 2>&1' not in script
    assert '"$BACKEND_LOG"' not in script
    assert '"$FRONTEND_LOG"' not in script
