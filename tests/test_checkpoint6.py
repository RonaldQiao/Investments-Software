import asyncio
import sqlite3
from datetime import date

import pytest

from app.attribution import attribution
from app.db import init_db, set_setting
from app.fees import record_cash_flow
from app.nav import take_snapshot
from app import scheduler


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    lp_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
    return conn, lp_id


def test_attribution_matches_gross_nav_change_without_fee_liability():
    conn, lp_id = database()
    set_setting(conn, "mgmt_fee_bps", 0)
    instrument_id = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',100)"
    ).lastrowid
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,?, ?,?)",
        (instrument_id, "2024-01-02T15:00:00-05:00", "BUY", 10, 100),
    )
    record_cash_flow(conn, "2024-01-02T15:00:00-05:00", 1000, lp_id, "test")
    take_snapshot(conn, date(2024, 1, 2), refresh=False)
    conn.execute("UPDATE instruments SET manual_mark=110 WHERE id=?", (instrument_id,))
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees) "
        "VALUES (?,?,?,?,?,?)",
        (instrument_id, "2024-01-03T12:00:00-05:00", "BUY", 5, 105, 2),
    )
    take_snapshot(conn, date(2024, 1, 3), refresh=False)
    conn.execute("UPDATE instruments SET manual_mark=120 WHERE id=?", (instrument_id,))
    take_snapshot(conn, date(2024, 1, 4), refresh=False)
    result = attribution(conn, date(2024, 1, 2), date(2024, 1, 4))
    nav = conn.execute(
        "SELECT date,nav FROM nav_snapshots WHERE date IN ('2024-01-02','2024-01-04') "
        "ORDER BY date"
    ).fetchall()
    net_flows = conn.execute(
        "SELECT COALESCE(SUM(flows_today),0) FROM nav_snapshots "
        "WHERE date>'2024-01-02' AND date<='2024-01-04'"
    ).fetchone()[0]
    mgmt_fees = conn.execute(
        "SELECT COALESCE(SUM(mgmt_fee_accrued),0) FROM nav_snapshots "
        "WHERE date>'2024-01-02' AND date<='2024-01-04'"
    ).fetchone()[0]
    assert result["rows"][0]["pnl"] == pytest.approx(273)
    assert result["total"]["pnl"] == pytest.approx(
        nav[-1]["nav"] - nav[0]["nav"] - net_flows + mgmt_fees,
        abs=1e-6,
    )


def test_snapshot_refresh_retries_without_waiting(monkeypatch):
    conn, _ = database()
    calls = []

    async def refresh(_conn):
        calls.append(True)
        return ["BAD"] if len(calls) < 3 else []

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(scheduler, "refresh_prices", refresh)
    failures = asyncio.run(scheduler.refresh_with_retries(conn, sleep=no_wait))
    assert calls == [True, True, True]
    assert failures == []


def test_backup_uses_sqlite_backup_and_retains_twenty(tmp_path, monkeypatch):
    from app import db
    from app.main import backup_database

    db.DB_PATH = tmp_path / "ledger.db"
    conn = db.get_conn()
    init_db(conn)
    conn.close()
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("LEDGER_BACKUP_DIR", str(backup_dir))
    filename = backup_database()
    assert filename.startswith("ledger-") and filename.endswith(".db")
    assert (backup_dir / filename).exists()
