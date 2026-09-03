import sqlite3
from datetime import date

import pytest

from app.db import get_setting, init_db, set_setting
from app.fees import (
    crystallize_perf_fee,
    fee_scenario,
    ownership,
    record_cash_flow,
    settle_fees,
)
from app.nav import take_snapshot


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    principal = conn.execute(
        "SELECT id FROM lps WHERE name='Principal'"
    ).fetchone()["id"]
    return conn, principal


def test_cash_flows_issue_and_redeem_at_current_nav():
    conn, principal = database()
    instrument = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',100)"
    )
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,'BUY',10,100)",
        (instrument.lastrowid, "2026-01-01"),
    )
    first = record_cash_flow(conn, "2026-01-01", 1000, principal, "first")
    assert first["nav_per_unit"] == 1000
    take_snapshot(conn, date(2026, 1, 1), refresh=False)
    conn.execute("UPDATE instruments SET manual_mark=110")
    second = record_cash_flow(conn, "2026-01-02", 500, principal, "second")
    assert second["nav_per_unit"] == pytest.approx(1100)
    withdrawal = record_cash_flow(conn, "2026-01-02", -220, principal, "withdraw")
    assert withdrawal["units"] == pytest.approx(-0.2)
    assert conn.execute("SELECT COUNT(*) FROM lp_units").fetchone()[0] == 3


def test_withdrawal_cannot_exceed_lp_value():
    conn, principal = database()
    record_cash_flow(conn, "2026-01-01", 1000, principal, "first")
    with pytest.raises(ValueError, match="Withdrawal exceeds LP value"):
        record_cash_flow(conn, "2026-01-02", -1001, principal, "too much")


def test_ownership_sums_to_net_nav_after_settlement():
    conn, principal = database()
    gp = conn.execute("INSERT INTO lps(name,is_gp) VALUES ('GP',1)").lastrowid
    set_setting(conn, "mgmt_fee_bps", 0)
    record_cash_flow(conn, "2026-01-01", 1_000_000, principal, "capital")
    crystallize_setup = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',200)"
    )
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,'2026-01-01','BUY',1000,100)",
        (crystallize_setup.lastrowid,),
    )
    take_snapshot(conn, date(2026, 1, 1), refresh=False)
    set_setting(conn, "hwm_per_unit", 1000)
    fee = crystallize_perf_fee(conn, "2026-01-02")
    assert fee == pytest.approx(20_000)
    units = settle_fees(conn, "2026-01-02")
    assert units == pytest.approx(20_000 / 1080)
    assert float(conn.execute(
        "SELECT value FROM settings WHERE key='fee_liability'"
    ).fetchone()[0]) == 0
    data = ownership(conn)
    assert sum(row["percentage"] for row in data["rows"]) == pytest.approx(1)
    assert sum(row["value"] for row in data["rows"]) == pytest.approx(data["total_value"])


def test_performance_fee_respects_high_water_mark():
    conn, principal = database()
    set_setting(conn, "mgmt_fee_bps", 0)
    instrument = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',200)"
    )
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,'2026-01-01','BUY',1000,100)",
        (instrument.lastrowid,),
    )
    record_cash_flow(conn, "2026-01-01", 1_000_000, principal, "capital")
    take_snapshot(conn, date(2026, 1, 1), refresh=False)
    assert crystallize_perf_fee(conn, "2026-01-02") == pytest.approx(20_000)
    assert float(get_setting(conn, "hwm_per_unit", 0)) == 1100
    assert crystallize_perf_fee(conn, "2026-01-03") == 0
    conn.execute("UPDATE instruments SET manual_mark=150")
    assert crystallize_perf_fee(conn, "2026-01-04") == 0
    conn.execute("UPDATE instruments SET manual_mark=250")
    assert crystallize_perf_fee(conn, "2026-01-05") == pytest.approx(6_000)


def test_fee_scenario():
    result = fee_scenario(1_000_000, 1000, 1000, 10, 2, 20)
    assert result["gross_pnl"] == pytest.approx(100_000)
    assert result["mgmt_fee"] == pytest.approx(20_000)
    assert result["perf_fee"] == pytest.approx(16_000)
    assert result["net_to_lps"] == pytest.approx(1_064_000)
    assert result["net_return_pct"] == pytest.approx(6.4)
    assert result["gp_take"] == pytest.approx(36_000)
    assert result["new_hwm"] == pytest.approx(1064)


def test_performance_fee_uses_nav_net_of_management_liability():
    conn, principal = database()
    instrument = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',200)"
    )
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) "
        "VALUES (?,'2026-01-01','BUY',1000,100)",
        (instrument.lastrowid,),
    )
    record_cash_flow(conn, "2026-01-01", 1_000_000, principal, "capital")
    take_snapshot(conn, date(2026, 1, 1), refresh=False)
    mgmt_fee = 1_100_000 * 200 / 10000 / 252
    take_snapshot(conn, date(2026, 1, 2), refresh=False)
    expected = (1100 - mgmt_fee / 1000 - 1000) * 1000 * 0.2
    assert crystallize_perf_fee(conn, "2026-01-02") == pytest.approx(expected)
