import sqlite3
from datetime import date, timedelta

import pytest

from app.attribution import attribution
from app.db import init_db, set_setting
from app.fees import crystallize_perf_fee, ownership, record_cash_flow
from app.nav import take_snapshot


def _weekdays(start, count):
    days = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def test_month_long_fund_scenario_invariants_and_fee_attribution():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    set_setting(conn, "benchmark_symbol", "")
    set_setting(conn, "mgmt_fee_bps", 150)
    set_setting(conn, "perf_fee_pct", 20)

    conn.execute("UPDATE lps SET name='A', mgmt_fee_bps=100, perf_fee_pct=20 WHERE name='Principal'")
    a_id = conn.execute("SELECT id FROM lps WHERE name='A'").fetchone()["id"]
    b_id = conn.execute(
        "INSERT INTO lps(name,is_gp,mgmt_fee_bps,perf_fee_pct) VALUES ('B',0,200,0)"
    ).lastrowid
    gp_id = conn.execute("INSERT INTO lps(name,is_gp) VALUES ('GP',1)").lastrowid

    instruments = {}
    for symbol, name, asset_class, multiplier, mark in (
        ("EQ", "Equity", "equity", 1, 100),
        ("ETF", "ETF", "etf", 1, 200),
        ("BTC", "Bitcoin", "crypto", 1, 500),
        ("BOND", "Bond", "bond", 1, 1000),
        ("FUT", "Future", "future", 50, 1000),
        ("OTHER", "Other", "other", 1, 10),
    ):
        instruments[symbol] = conn.execute(
            "INSERT INTO instruments(symbol,name,asset_class,multiplier,pricing_source,"
            "manual_mark,manual_mark_at) VALUES (?,?,?,?,'manual',?,?)",
            (symbol, name, asset_class, multiplier, mark, "2026-01-02"),
        ).lastrowid
    conn.commit()

    days = _weekdays(date(2026, 1, 2), 22)
    first_day = days[0]
    opening_trades = (
        ("EQ", "BUY", 100, 100),
        ("ETF", "BUY", 50, 200),
        ("BTC", "BUY", 2, 500),
        ("BOND", "BUY", 10, 1000),
        ("FUT", "BUY", 2, 1000),
        ("OTHER", "BUY", 100, 10),
    )
    for symbol, side, quantity, price in opening_trades:
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
            "VALUES (?,?,?,?,?,0,'month scenario')",
            (instruments[symbol], f"{first_day.isoformat()}T12:00:00", side, quantity, price),
        )
    record_cash_flow(conn, f"{first_day.isoformat()}T12:00:00", 150_000, a_id, "opening A")
    record_cash_flow(conn, f"{first_day.isoformat()}T12:00:00", 100_000, b_id, "opening B")
    record_cash_flow(conn, f"{first_day.isoformat()}T12:00:00", 50_000, gp_id, "opening GP")
    conn.commit()

    trade_days = {
        1: (("EQ", "SELL", 150, 102),),
        2: (("EQ", "BUY", 50, 98),),
        3: (("ETF", "SELL", 25, 205),),
        4: (("FUT", "SELL", 1, 1012),),
        6: (("BTC", "SELL", 2, 530),),
        8: (("BOND", "SELL", 10, 1016),),
        11: (("EQ", "BUY", 20, 105),),
        14: (("EQ", "SELL", 20, 110),),
    }
    base_marks = {"EQ": 100, "ETF": 200, "BTC": 500, "BOND": 1000, "FUT": 1000, "OTHER": 10}
    for index, day in enumerate(days):
        for symbol, base in base_marks.items():
            mark = base + {
                "EQ": 0.45,
                "ETF": 1.4,
                "BTC": 5,
                "BOND": 2,
                "FUT": 3,
                "OTHER": 0.1,
            }[symbol] * index
            conn.execute(
                "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
                (mark, day.isoformat(), instruments[symbol]),
            )
        for symbol, side, quantity, price in trade_days.get(index, ()):
            conn.execute(
                "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
                "VALUES (?,?,?,?,?,0,'month scenario')",
                (instruments[symbol], f"{day.isoformat()}T12:00:00", side, quantity, price),
            )
        if index == 10:
            record_cash_flow(conn, f"{day.isoformat()}T12:00:00", 20_000, b_id, "mid-month contribution")
        if index == 13:
            record_cash_flow(conn, f"{day.isoformat()}T12:00:00", -10_000, a_id, "mid-month withdrawal")
        conn.commit()
        take_snapshot(conn, day, "scheduled", refresh=False)

    snapshots = conn.execute(
        "SELECT date,nav,cash,net_exposure,flows_today,units_outstanding,nav_per_unit,"
        "daily_return FROM nav_snapshots ORDER BY date"
    ).fetchall()
    assert len(snapshots) == 22
    assert all(
        row["units_outstanding"] * row["nav_per_unit"] == pytest.approx(row["nav"], abs=1e-9)
        for row in snapshots
    )

    compounded = 1.0
    for row in snapshots:
        if row["daily_return"] is not None:
            compounded *= 1 + row["daily_return"]
    assert compounded - 1 == pytest.approx(
        snapshots[-1]["nav_per_unit"] / snapshots[0]["nav_per_unit"] - 1,
        abs=1e-9,
    )

    result = attribution(conn, snapshots[0]["date"], snapshots[-1]["date"])
    gross_start = snapshots[0]["cash"] + snapshots[0]["net_exposure"]
    gross_end = snapshots[-1]["cash"] + snapshots[-1]["net_exposure"]
    flows = sum(row["flows_today"] for row in snapshots[1:])
    assert result["total"]["pnl"] == pytest.approx(gross_end - gross_start - flows, abs=1e-6)

    assert conn.execute("SELECT COUNT(*) FROM lp_fee_accruals").fetchone()[0] >= 42
    assert conn.execute("SELECT COALESCE(SUM(mgmt),0) FROM lp_fee_accruals").fetchone()[0] > 0

    before = {row["id"]: row["hwm"] for row in ownership(conn)["rows"]}
    gp_before = next(row["units"] for row in ownership(conn)["rows"] if row["id"] == gp_id)
    navpu_before = ownership(conn)["nav_per_unit"]
    total_fee = crystallize_perf_fee(conn, days[-1])
    after_ownership = ownership(conn)
    gp_after = next(row["units"] for row in after_ownership["rows"] if row["id"] == gp_id)
    paid_lp_ids = {
        row["lp_id"]
        for row in conn.execute("SELECT lp_id FROM fee_events WHERE kind='perf'").fetchall()
    }

    assert sum(row["percentage"] for row in after_ownership["rows"]) == pytest.approx(1)
    assert gp_after - gp_before == pytest.approx(total_fee / navpu_before, abs=1e-9)
    assert after_ownership["nav_per_unit"] == pytest.approx(navpu_before, abs=1e-9)
    for row in after_ownership["rows"]:
        if row["id"] in paid_lp_ids:
            assert row["hwm"] > before[row["id"]]
        elif row["id"] != gp_id:
            assert row["hwm"] == pytest.approx(before[row["id"]])
