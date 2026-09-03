import random
import sqlite3
from datetime import date, timedelta

import pytest

from app.db import init_db, set_setting
from app.fees import record_cash_flow
from app.nav import take_snapshot


def test_random_walk_cumulative_returns_and_nav_unit_identity():
    rng = random.Random(1)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    set_setting(conn, "mgmt_fee_bps", 200)
    lp_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
    investor_id = conn.execute(
        "INSERT INTO lps(name,is_gp) VALUES ('Investor A',0)"
    ).lastrowid
    record_cash_flow(conn, "2026-01-01T12:00:00", 100_000, lp_id, "seed")
    record_cash_flow(conn, "2026-01-01T12:00:00", 50_000, investor_id, "seed")
    marks = {}
    shadow_npu = 1000.0
    shadow_units = 150.0
    shadow_gross_nav = 150_000.0
    shadow_liability = 0.0
    quantities = {}
    snapshots = []

    for day_number in range(40):
        day = date(2026, 1, 2) + timedelta(days=day_number)
        if day_number == 0:
            day = date(2026, 1, 2)
        existing = list(marks)
        mark_pnl = 0.0
        for instrument_id in existing:
            previous_mark = marks[instrument_id]
            marks[instrument_id] *= 1 + rng.uniform(-0.03, 0.03)
            mark_pnl += quantities[instrument_id] * (
                marks[instrument_id] - previous_mark
            )
            conn.execute(
                "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
                (marks[instrument_id], day.isoformat(), instrument_id),
            )
        for _ in range(rng.randint(0, 2)):
            symbol = f"R{day_number}_{rng.randint(0, 9999)}"
            mark = rng.uniform(10, 200)
            instrument_id = conn.execute(
                "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark,"
                "manual_mark_at) VALUES (?,?,?,'manual',?,?)",
                (symbol, symbol, "equity", mark, day.isoformat()),
            ).lastrowid
            marks[instrument_id] = mark
            if rng.random() < 0.8:
                quantities[instrument_id] = 1.0
                conn.execute(
                    "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,"
                    "'BUY',?,?)",
                    (instrument_id, f"{day.isoformat()}T12:00:00", 1, mark),
                )
            else:
                quantities[instrument_id] = 0.0
        if rng.random() < 0.2:
            amount = rng.uniform(100, 5000)
            flow_lp_id = rng.choice((lp_id, investor_id))
            if rng.random() < 0.5:
                record_cash_flow(
                    conn, f"{day.isoformat()}T12:00:00", amount, flow_lp_id, "random"
                )
            else:
                balance = conn.execute(
                    "SELECT COALESCE(SUM(units),0) FROM lp_units WHERE lp_id=?",
                    (flow_lp_id,),
                ).fetchone()[0]
                if balance * shadow_npu > amount:
                    record_cash_flow(
                        conn, f"{day.isoformat()}T12:00:00", -amount, lp_id, "random"
                    )
        if rng.random() < 0.1:
            zero = [
                instrument_id
                for instrument_id in marks
                if not conn.execute(
                    "SELECT 1 FROM trades WHERE instrument_id=? LIMIT 1", (instrument_id,)
                ).fetchone()
            ]
            if zero:
                instrument_id = zero[0]
                del marks[instrument_id]
                del quantities[instrument_id]
                conn.execute("DELETE FROM instruments WHERE id=?", (instrument_id,))
        conn.commit()
        if day_number:
            shadow_gross_nav += mark_pnl
            shadow_npu_before_flow = (
                shadow_gross_nav - shadow_liability
            ) / shadow_units
            for flow in conn.execute(
                "SELECT amount FROM cash_flows WHERE substr(ts,1,10)=?", (day.isoformat(),)
            ).fetchall():
                amount = float(flow["amount"])
                shadow_gross_nav += amount
                shadow_units += amount / shadow_npu_before_flow
            fee = shadow_gross_nav * 200 / 10000 / 252
            shadow_liability += fee
            shadow_npu = (shadow_gross_nav - shadow_liability) / shadow_units
        snapshots.append(take_snapshot(conn, day, refresh=False))
        assert snapshots[-1]["nav_per_unit"] == pytest.approx(shadow_npu, abs=1e-6)

    assert all(
        snapshot["units_outstanding"] * snapshot["nav_per_unit"]
        == pytest.approx(snapshot["nav"], abs=1e-9)
        for snapshot in snapshots
    )
    cumulative = 1.0
    for snapshot in snapshots:
        if snapshot["daily_return"] is not None:
            cumulative *= 1 + snapshot["daily_return"]
    unit_return = snapshots[-1]["nav_per_unit"] / snapshots[0]["nav_per_unit"] - 1
    assert cumulative - 1 == pytest.approx(unit_return, abs=1e-9)
