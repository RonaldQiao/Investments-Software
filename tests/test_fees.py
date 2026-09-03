import sqlite3
from datetime import date

import pytest

from app.db import get_setting, init_db, set_setting
from app.fees import (
    accrue_management_fees,
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


def test_management_fees_use_per_lp_terms_and_exclude_gp():
    conn, principal = database()
    investor = conn.execute(
        "INSERT INTO lps(name,is_gp,mgmt_fee_bps) VALUES ('Investor',0,100)"
    ).lastrowid
    gp = conn.execute("INSERT INTO lps(name,is_gp,mgmt_fee_bps) VALUES ('GP',1,900)").lastrowid
    record_cash_flow(conn, "2026-01-01", 100_000, principal, "default")
    record_cash_flow(conn, "2026-01-01", 100_000, investor, "custom")
    record_cash_flow(conn, "2026-01-01", 100_000, gp, "gp")
    fee = accrue_management_fees(conn, "2026-01-02", 300_000, 1000)
    assert fee == pytest.approx(100_000 * 200 / 10000 / 252 + 100_000 * 100 / 10000 / 252)
    rows = conn.execute("SELECT lp_id,mgmt,perf FROM lp_fee_accruals").fetchall()
    assert {row["lp_id"] for row in rows} == {principal, investor}
    assert all(row["perf"] == 0 for row in rows)


def test_withdrawal_cannot_exceed_lp_value():
    conn, principal = database()
    record_cash_flow(conn, "2026-01-01", 1000, principal, "first")
    with pytest.raises(ValueError, match="Withdrawal exceeds LP value"):
        record_cash_flow(conn, "2026-01-02", -1001, principal, "too much")


def test_ownership_sums_to_net_nav_after_settlement():
    conn, principal = database()
    conn.execute("INSERT INTO lps(name,is_gp) VALUES ('GP',1)")
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
    assert units == 0
    assert float(get_setting(conn, "fee_liability", 0) or 0) == 0
    data = ownership(conn)
    assert sum(row["percentage"] for row in data["rows"]) == pytest.approx(1)
    assert sum(row["value"] for row in data["rows"]) == pytest.approx(data["total_value"])


def test_performance_fee_respects_high_water_mark():
    conn, principal = database()
    conn.execute("INSERT INTO lps(name,is_gp) VALUES ('GP',1)")
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
    assert crystallize_perf_fee(conn, "2026-01-05") == pytest.approx(9_818.181818)


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
    conn.execute("INSERT INTO lps(name,is_gp) VALUES ('GP',1)")
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


def test_per_lp_hwm_charges_only_lp_above_its_own_hwm():
    conn, principal = database()
    gp = conn.execute("INSERT INTO lps(name,is_gp) VALUES ('GP',1)").lastrowid
    set_setting(conn, "mgmt_fee_bps", 0)
    investor = conn.execute(
        "INSERT INTO lps(name,is_gp,perf_fee_pct) VALUES ('Later',0,20)"
    ).lastrowid
    instrument = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('TEST','Test','equity','manual',100)"
    ).lastrowid
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) "
        "VALUES (?, '2026-01-01', 'BUY', 100, 100)",
        (instrument,),
    )
    record_cash_flow(conn, "2026-01-01", 10_000, principal, "first")
    take_snapshot(conn, date(2026, 1, 1), refresh=False)
    conn.execute("UPDATE instruments SET manual_mark=80")
    take_snapshot(conn, date(2026, 1, 2), refresh=False)
    record_cash_flow(conn, "2026-01-02", 8_000, investor, "later")
    conn.execute("UPDATE instruments SET manual_mark=100")
    before = ownership(conn)
    fee = crystallize_perf_fee(conn, "2026-01-03")
    after = ownership(conn)
    principal_row = next(row for row in after["rows"] if row["id"] == principal)
    investor_row = next(row for row in after["rows"] if row["id"] == investor)
    gp_row = next(row for row in after["rows"] if row["id"] == gp)
    assert fee == pytest.approx(200)
    assert next(row for row in before["rows"] if row["id"] == principal)["hwm"] == 1000
    assert principal_row["hwm"] == 1000
    assert investor_row["hwm"] == 900
    assert gp_row["units"] == pytest.approx(200 / 900)
    assert after["nav_per_unit"] == pytest.approx(before["nav_per_unit"], abs=1e-9)
    assert after["total_units"] == pytest.approx(before["total_units"], abs=1e-9)
