import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_launchd_script_generates_plists_without_bootstrapping(tmp_path):
    agents = tmp_path / "agents"
    logs = tmp_path / "logs"
    env = os.environ | {
        "LEDGER_LAUNCH_AGENTS_DIR": str(agents),
        "LEDGER_LOG_DIR": str(logs),
        "LEDGER_SKIP_BOOTSTRAP": "1",
    }
    result = subprocess.run(
        ["bash", "scripts/install_launchd.sh"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    server = (agents / "com.ledger.server.plist").read_text()
    snapshot = (agents / "com.ledger.snapshot.plist").read_text()
    assert "<string>app.main:app</string>" in server
    assert "<integer>5</integer>" in snapshot
    assert "<string>--catch-up</string>" in snapshot
