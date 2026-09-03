import json
import sqlite3
from datetime import date, datetime, timezone
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, set_setting
from app.nav import compute_portfolio, exposure_by_class, take_snapshot


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_failed_and_stale_prices_are_reported():
    conn = memory_db()
    instrument_id = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source) "
        "VALUES ('YHOO','Yahoo','equity','yahoo')"
    ).lastrowid
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,'BUY',2,10)",
        (instrument_id, "2026-01-01"),
    )
    old = datetime.now(timezone.utc).replace(year=2025).isoformat()
    conn.execute(
        "INSERT INTO prices(instrument_id,price,ts,source) VALUES (?,?,?,'yahoo')",
        (instrument_id, 12, old),
    )
    set_setting(conn, "last_refresh_failures", json.dumps(["YHOO"]))
    conn.commit()
    position = compute_portfolio(conn)["positions"][0]
    assert position["price_failed"] is True
    assert position["mark"] is None
    assert position["price_age_seconds"] > 86400


def test_snapshot_falls_back_and_audits_marks():
    conn = memory_db()
    instrument_id = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,pricing_source) "
        "VALUES ('YHOO','Yahoo','equity','yahoo')"
    ).lastrowid
    lp_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,'BUY',2,10)",
        (instrument_id, "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO cash_flows(ts,amount,lp_id) VALUES ('2026-01-01',1000,?)",
        (lp_id,),
    )
    set_setting(conn, "last_refresh_failures", json.dumps(["YHOO"]))
    conn.commit()
    take_snapshot(conn, date(2026, 1, 2), refresh=False)
    mark = conn.execute(
        "SELECT mark,source FROM snapshot_marks WHERE instrument_id=?",
        (instrument_id,),
    ).fetchone()
    assert mark["mark"] == 10
    assert mark["source"] == "fallback"
    conn.execute(
        "INSERT OR REPLACE INTO prices(instrument_id,price,ts,source) "
        "VALUES (?,20,?,'yahoo')",
        (instrument_id, datetime.now(timezone.utc).isoformat()),
    )
    set_setting(conn, "last_refresh_failures", "[]")
    conn.commit()
    take_snapshot(conn, date(2026, 1, 3), refresh=False)
    conn.execute("DELETE FROM prices WHERE instrument_id=?", (instrument_id,))
    set_setting(conn, "last_refresh_failures", json.dumps(["YHOO"]))
    conn.commit()
    take_snapshot(conn, date(2026, 1, 4), refresh=False)
    mark = conn.execute(
        "SELECT mark,source FROM snapshot_marks WHERE date='2026-01-04' AND instrument_id=?",
        (instrument_id,),
    ).fetchone()
    assert mark["mark"] == 20
    assert mark["source"] == "snapshot"


def test_exposure_by_class_groups_and_sorts():
    portfolio = {
        "net_nav": 1000,
        "positions": [
            {"asset_class": "equity", "market_value": 600, "unrealized": 10},
            {"asset_class": "equity", "market_value": -100, "unrealized": -5},
            {"asset_class": "crypto", "market_value": 800, "unrealized": 20},
        ],
    }
    rows = exposure_by_class(portfolio)
    assert [row["asset_class"] for row in rows] == ["crypto", "equity", "Total"]
    assert rows[0]["gross"] == 800
    assert rows[1]["long"] == 600
    assert rows[1]["short"] == 100
    assert rows[-1]["gross"] == 1500
    assert rows[-1]["pct_nav"] == 1.5


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
    from app import db

    db.DB_PATH = tmp_path / "ledger.db"
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_trade_csv_round_trip_and_edit_warning(client, tmp_path):
    response = client.post(
        "/instruments",
        data={
            "symbol": "TEST",
            "name": "Test",
            "asset_class": "equity",
            "pricing_source": "manual",
            "manual_mark": "10",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    instrument = client.get("/api/instruments").json()[0]
    client.post(
        "/trades",
        data={
            "instrument_id": instrument["id"],
            "ts": "2026-01-01",
            "side": "BUY",
            "quantity": "5",
            "price": "10",
            "fees": "0",
            "notes": "round trip",
        },
        follow_redirects=False,
    )
    exported = client.get("/trades.csv")
    assert exported.status_code == 200
    assert exported.text.splitlines()[0] == "ts,symbol,side,quantity,price,fees,notes"
    original_positions = client.get("/api/portfolio").json()["positions"]
    from app import db
    from app.main import app

    original_db = db.DB_PATH
    db.DB_PATH = tmp_path / "fresh.db"
    with TestClient(app) as fresh_client:
        fresh_client.post(
            "/instruments",
            data={
                "symbol": "TEST",
                "name": "Test",
                "asset_class": "equity",
                "pricing_source": "manual",
                "manual_mark": "10",
            },
            follow_redirects=False,
        )
        imported = fresh_client.post(
            "/trades/import",
            files={"file": ("trades.csv", exported.content, "text/csv")},
            follow_redirects=False,
        )
        assert imported.status_code == 303
        assert "ok=" in imported.headers["location"]
        assert fresh_client.get("/trades").status_code == 200
        fresh_positions = fresh_client.get("/api/portfolio").json()["positions"]
    db.DB_PATH = original_db
    assert fresh_positions == original_positions
    edited = client.post(
        f"/instruments/{instrument['id']}/edit",
        data={
            "name": "Test",
            "asset_class": "equity",
            "multiplier": "2",
            "pricing_source": "manual",
            "yahoo_symbol": "",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert "multiplier changed" in unquote(edited.headers["location"])


def test_import_unknown_symbol_and_oversized_withdrawal_flash(client):
    unknown = client.post(
        "/trades/import",
        files={
            "file": (
                "bad.csv",
                b"ts,symbol,side,quantity,price,fees,notes\n2026-01-01,NOPE,BUY,1,10,0,\n",
                "text/csv",
            )
        },
        follow_redirects=False,
    )
    assert unknown.status_code == 303
    assert "error=" in unknown.headers["location"]
    client.post(
        "/capital",
        data={"lp_id": "1", "flow_type": "Contribution", "amount": "1000"},
        follow_redirects=False,
    )
    withdrawal = client.post(
        "/capital",
        data={"lp_id": "1", "flow_type": "Withdrawal", "amount": "1001"},
        follow_redirects=False,
    )
    assert withdrawal.status_code == 303
    assert "error=" in withdrawal.headers["location"]
