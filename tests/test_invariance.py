import random
import sqlite3
from datetime import date

import pytest

from app.db import init_db, set_setting
from app.fees import record_cash_flow
from app.nav import compute_portfolio, take_snapshot


def book():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    set_setting(conn, "benchmark_symbol", "")
    set_setting(conn, "mgmt_fee_bps", 0)
    principal = conn.execute(
        "SELECT id FROM lps WHERE name='Principal'"
    ).fetchone()["id"]
    instrument = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
        "VALUES ('BASE','Base','equity','manual',100)"
    )
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,'2026-01-01','BUY',100,100)",
        (instrument.lastrowid,),
    )
    record_cash_flow(conn, "2026-01-01", 100_000, principal, "base")
    take_snapshot(conn, date(2026, 1, 1), refresh=False)
    return conn, principal, instrument.lastrowid


def add_day_two_changes(conn, principal):
    rng = random.Random(0)
    for index in range(5):
        mark = round(rng.uniform(20, 200), 2)
        instrument = conn.execute(
            "INSERT INTO instruments(symbol,name,asset_class,pricing_source,manual_mark) "
            "VALUES (?,?,?,?,?)",
            (f"NEW{index}", f"New {index}", "equity", "manual", mark),
        )
        quantity = rng.randint(1, 20)
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,'BUY',?,?)",
            (instrument.lastrowid, "2026-01-02", quantity, mark),
        )
    conn.commit()
    record_cash_flow(conn, "2026-01-02", 250_000, principal, "addition")
    record_cash_flow(conn, "2026-01-02", -50_000, principal, "redemption")


def test_returns_are_invariant_to_trades_and_flows_at_marks():
    baseline, baseline_lp, baseline_instrument = book()
    expanded, expanded_lp, expanded_instrument = book()
    record_cash_flow(baseline, "2026-01-02", 250_000, baseline_lp, "addition")
    record_cash_flow(baseline, "2026-01-02", -50_000, baseline_lp, "redemption")
    add_day_two_changes(expanded, expanded_lp)
    baseline_day_two = take_snapshot(baseline, date(2026, 1, 2), refresh=False)
    expanded_day_two = take_snapshot(expanded, date(2026, 1, 2), refresh=False)
    assert expanded_day_two["daily_return"] == pytest.approx(
        baseline_day_two["daily_return"], abs=1e-9
    )
    baseline.execute("UPDATE instruments SET manual_mark=110 WHERE id=?", (baseline_instrument,))
    expanded.execute("UPDATE instruments SET manual_mark=110 WHERE id=?", (expanded_instrument,))
    baseline_day_three = take_snapshot(baseline, date(2026, 1, 3), refresh=False)
    expanded_day_three = take_snapshot(expanded, date(2026, 1, 3), refresh=False)
    assert expanded_day_three["daily_return"] == pytest.approx(
        baseline_day_three["daily_return"], abs=1e-9
    )
    for conn in (baseline, expanded):
        portfolio = compute_portfolio(conn)
        assert portfolio["cash"] + portfolio["net_exposure"] == pytest.approx(
            portfolio["gross_nav"], abs=1e-9
        )
