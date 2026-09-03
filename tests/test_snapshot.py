import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from app.db import init_db

ROOT = Path(__file__).resolve().parent.parent


def test_snapshot_cli_writes_snapshot_and_job_log(tmp_path):
    db_path = tmp_path / "ledger.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("UPDATE settings SET value='' WHERE key='benchmark_symbol'")
    principal_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
    instrument_id = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',100)"
    ).lastrowid
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,?,?,?)",
        (instrument_id, "2024-01-02T12:00:00-05:00", "BUY", 10, 100),
    )
    conn.execute(
        "INSERT INTO cash_flows(ts,amount,lp_id,note) VALUES (?,?,?,?)",
        ("2024-01-02T12:00:00-05:00", 1000, principal_id, "test"),
    )
    conn.commit()
    conn.close()

    env = os.environ | {"LEDGER_DB": str(db_path), "LEDGER_NO_SCHEDULER": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "app.snapshot", "--date", "2024-01-02"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("snapshot 2024-01-02 nav=")
    check = sqlite3.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM nav_snapshots").fetchone()[0] == 1
    assert check.execute("SELECT job,status FROM job_log").fetchone() == ("cli", "ok")
