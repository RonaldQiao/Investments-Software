import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.attribution import attribution
from app.db import init_db, set_setting
from app.fees import record_cash_flow
from app.nav import take_snapshot


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
    from app import db

    db.DB_PATH = tmp_path / "ledger.db"
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def database():
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
        "VALUES ('TEST','Test','equity','manual',100)"
    ).lastrowid
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) "
        "VALUES (?,?,'BUY',10,100)",
        (instrument, "2026-01-01"),
    )
    record_cash_flow(conn, "2026-01-01", 10_000, principal, "opening")
    conn.commit()
    return conn, principal


def test_cash_adjustment_changes_nav_and_preserves_contribution_navpu():
    conn, principal = database()
    take_snapshot(conn, date(2026, 1, 1), refresh=False)
    conn.execute(
        "INSERT INTO cash_adjustments(ts,amount,category,note) "
        "VALUES ('2026-01-02',500,'dividend','cash dividend')"
    )
    conn.commit()
    take_snapshot(conn, date(2026, 1, 2), refresh=False)
    before = conn.execute(
        "SELECT nav,nav_per_unit,units_outstanding FROM nav_snapshots WHERE date='2026-01-02'"
    ).fetchone()
    assert before["nav"] == pytest.approx(10_500)
    assert before["nav_per_unit"] == pytest.approx(1050)
    assert before["units_outstanding"] == pytest.approx(10)

    record_cash_flow(conn, "2026-01-03", 1000, principal, "later contribution")
    take_snapshot(conn, date(2026, 1, 3), refresh=False)
    after = conn.execute(
        "SELECT nav,nav_per_unit,units_outstanding FROM nav_snapshots WHERE date='2026-01-03'"
    ).fetchone()
    assert after["nav"] == pytest.approx(11_500)
    assert after["nav_per_unit"] == pytest.approx(1050)
    assert after["units_outstanding"] == pytest.approx(10 + 1000 / 1050)


def test_cash_adjustment_attribution_and_delete():
    conn, _ = database()
    take_snapshot(conn, date(2026, 1, 1), refresh=False)
    conn.execute(
        "INSERT INTO cash_adjustments(ts,amount,category,note) "
        "VALUES ('2026-01-02',500,'dividend','cash dividend')"
    )
    adjustment_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    take_snapshot(conn, date(2026, 1, 2), refresh=False)
    result = attribution(conn, "2026-01-01", "2026-01-02")
    dividend = next(row for row in result["rows"] if row["symbol"] == "Dividends")
    assert dividend["pnl"] == pytest.approx(500)
    assert next(row for row in result["classes"] if row["asset_class"] == "cash")[
        "pnl"
    ] == pytest.approx(500)
    assert result["total"]["pnl"] == pytest.approx(500)

    conn.execute("DELETE FROM cash_adjustments WHERE id=?", (adjustment_id,))
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM cash_adjustments WHERE id=?", (adjustment_id,)
    ).fetchone()[0] == 0


def test_cash_adjustment_routes_create_and_delete(client):
    response = client.post(
        "/capital/adjustments",
        data={
            "flow_date": "2026-01-02",
            "category": "dividend",
            "amount": "500",
            "note": "cash dividend",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    from app import db

    conn = db.get_conn()
    adjustment_id = conn.execute(
        "SELECT id FROM cash_adjustments"
    ).fetchone()["id"]
    conn.close()
    response = client.post(
        f"/capital/adjustments/{adjustment_id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = db.get_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM cash_adjustments WHERE id=?", (adjustment_id,)
    ).fetchone()[0] == 0
    conn.close()
