from datetime import date
import sqlite3

import pytest

from app.db import init_db, set_setting
from app.fees import record_cash_flow
from app.nav import take_snapshot


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    lp_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
    return conn, lp_id


def test_first_snapshot_issues_units_at_inception():
    conn, lp_id = database()
    conn.execute(
        "INSERT INTO cash_flows(ts,amount,lp_id,note) VALUES ('2024-01-02T12:00:00',1000000,?,'test')",
        (lp_id,),
    )
    snapshot = take_snapshot(conn, date(2024, 1, 2), "manual", refresh=False)
    assert snapshot["nav_per_unit"] == 1000
    assert snapshot["units_outstanding"] == 1000


def test_second_snapshot_has_mark_and_levered_returns():
    conn, lp_id = database()
    set_setting(conn, "mgmt_fee_bps", 0)
    instrument = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',100)"
    )
    instrument_id = instrument.lastrowid
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,'BUY',10,100)",
        (instrument_id, "2024-01-02T12:00:00"),
    )
    record_cash_flow(conn, "2024-01-02T12:00:00", 1000, lp_id, "test")
    take_snapshot(conn, date(2024, 1, 2), "manual", refresh=False)
    conn.execute("UPDATE instruments SET manual_mark=110 WHERE id=?", (instrument_id,))
    set_setting(conn, "leverage", 2)
    set_setting(conn, "borrow_rate", 0.05)
    snapshot = take_snapshot(conn, date(2024, 1, 3), "manual", refresh=False)
    assert snapshot["daily_return"] == pytest.approx(0.1)
    assert snapshot["levered_return"] == pytest.approx(0.2 - 0.05 / 252)


def test_resnapshot_does_not_double_accrue_management_fee():
    conn, lp_id = database()
    conn.execute(
        "INSERT INTO cash_flows(ts,amount,lp_id,note) VALUES ('2024-01-02T12:00:00',1000000,?,'test')",
        (lp_id,),
    )
    take_snapshot(conn, date(2024, 1, 2), "manual", refresh=False)
    first = take_snapshot(conn, date(2024, 1, 3), "manual", refresh=False)
    events = conn.execute("SELECT COUNT(*) count FROM fee_events").fetchone()["count"]
    liability = conn.execute(
        "SELECT value FROM settings WHERE key='fee_liability'"
    ).fetchone()["value"]
    second = take_snapshot(conn, date(2024, 1, 3), "manual", refresh=False)
    assert conn.execute("SELECT COUNT(*) count FROM fee_events").fetchone()["count"] == events
    assert conn.execute("SELECT value FROM settings WHERE key='fee_liability'").fetchone()["value"] == liability
    assert second["mgmt_fee_accrued"] == first["mgmt_fee_accrued"]


def test_cash_flow_issues_units_without_changing_return():
    conn, lp_id = database()
    set_setting(conn, "mgmt_fee_bps", 0)
    instrument = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',100)"
    )
    instrument_id = instrument.lastrowid
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,'BUY',10,100)",
        (instrument_id, "2024-01-02T12:00:00"),
    )
    record_cash_flow(conn, "2024-01-02T12:00:00", 1000, lp_id, "test")
    take_snapshot(conn, date(2024, 1, 2), "manual", refresh=False)
    conn.execute("UPDATE instruments SET manual_mark=110 WHERE id=?", (instrument_id,))
    record_cash_flow(conn, "2024-01-03T12:00:00", 500000, lp_id, "additional")
    snapshot = take_snapshot(conn, date(2024, 1, 3), "manual", refresh=False)
    assert snapshot["daily_return"] == pytest.approx(0.1)
