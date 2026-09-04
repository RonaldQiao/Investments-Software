import asyncio
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db
from app.fx import fx_rate_for
from app.nav import compute_portfolio, take_snapshot
from app.positions import build_positions
from app.pricing import fetch_fx_rates, refresh_prices


@pytest.fixture()
def fx_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
    db.DB_PATH = tmp_path / "ledger.db"
    from app.main import app

    with TestClient(app) as client:
        yield client


def _eur_instrument(conn):
    cursor = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
        "manual_mark) VALUES (?,?,?,?,?,?,?)",
        ("EUR", "Euro asset", "equity", "EUR", 1, "manual", 50),
    )
    conn.execute(
        "INSERT INTO fx_rates(currency,rate,ts,source) VALUES ('EUR',1.2,'now','manual')"
    )
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fx_rate) "
        "VALUES (?,?,?,?,?,?)",
        (cursor.lastrowid, "2026-01-01T12:00:00+00:00", "BUY", 100, 50, 1.1),
    )
    conn.commit()
    return cursor.lastrowid


def test_eur_accounting_and_fx_move(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    db.DB_PATH = tmp_path / "ledger.db"
    conn = db.get_conn()
    db.init_db(conn)
    db.set_setting(conn, "mgmt_fee_bps", 0)
    instrument_id = _eur_instrument(conn)
    portfolio = compute_portfolio(conn)
    position = portfolio["positions"][0]
    assert portfolio["cash"] == pytest.approx(-5500)
    assert position["market_value"] == pytest.approx(6000)
    assert position["unrealized"] == pytest.approx(500)
    assert portfolio["nav"] == pytest.approx(500)
    take_snapshot(conn, date(2026, 1, 1), fetch_benchmark=False)
    conn.execute("UPDATE fx_rates SET rate=1.3 WHERE currency='EUR'")
    conn.commit()
    take_snapshot(conn, date(2026, 1, 2), fetch_benchmark=False)
    snapshots = conn.execute(
        "SELECT nav,nav_per_unit,flows_today FROM nav_snapshots ORDER BY date"
    ).fetchall()
    assert snapshots[1]["nav"] - snapshots[0]["nav"] == pytest.approx(500)
    assert snapshots[1]["nav_per_unit"] != snapshots[0]["nav_per_unit"]
    assert snapshots[1]["flows_today"] == 0
    assert instrument_id
    conn.close()


def test_realized_fx_pnl_and_missing_fx(tmp_path, monkeypatch, fx_client):
    conn = db.get_conn()
    instrument_id = _eur_instrument(conn)
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fx_rate) "
        "VALUES (?,?,?,?,?,?)",
        (instrument_id, "2026-01-02T12:00:00+00:00", "SELL", 100, 55, 1.25),
    )
    conn.commit()
    assert build_positions(
        conn.execute("SELECT t.*,1 multiplier FROM trades t").fetchall()
    )[instrument_id].realized_pnl == pytest.approx((55 * 1.25 - 50 * 1.1) * 100)
    nav_before_missing = compute_portfolio(conn)["nav"]
    missing = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
        "manual_mark) VALUES ('GBP','Pound asset','equity','GBP',1,'manual',10)"
    ).lastrowid
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price) VALUES (?,?,?,?,?)",
        (missing, "2026-01-01", "BUY", 1, 10),
    )
    conn.commit()
    portfolio = compute_portfolio(conn)
    row = next(row for row in portfolio["positions"] if row["instrument_id"] == missing)
    assert row["fx_missing"] is True
    assert portfolio["nav"] == pytest.approx(nav_before_missing - 10)
    conn.close()
    assert fx_client.get("/positions").status_code == 200


def test_fx_fetch_inverts_and_manual_override_sticks(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    db.DB_PATH = tmp_path / "ledger.db"
    conn = db.get_conn()
    db.init_db(conn)
    instrument = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source) "
        "VALUES ('EUR','Euro','equity','EUR',1,'yahoo')"
    ).lastrowid
    conn.execute(
        "INSERT INTO fx_rates(currency,rate,ts,source) VALUES ('EUR',1.2,'now','manual')"
    )
    conn.commit()

    calls = []

    async def fake_fetch(symbols):
        calls.append(symbols)
        return {"EURUSD=X": 0.8} if "EURUSD=X" in symbols else {"EURUSD=X": None}

    monkeypatch.setattr("app.pricing.fetch_yahoo", fake_fetch)
    assert asyncio.run(fetch_fx_rates(["USD"], "EUR"))["USD"] == pytest.approx(1.25)
    asyncio.run(refresh_prices(conn))
    assert fx_rate_for(conn, "EUR") == pytest.approx(1.2)
    conn.execute("DELETE FROM fx_rates WHERE currency='EUR'")
    conn.commit()
    asyncio.run(refresh_prices(conn))
    assert fx_rate_for(conn, "EUR") == pytest.approx(0.8)
    assert calls
    assert instrument
    conn.close()


def test_manual_currency_refreshes_fx(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    db.DB_PATH = tmp_path / "ledger.db"
    conn = db.get_conn()
    db.init_db(conn)
    conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source) "
        "VALUES ('BOND','Euro bond','bond','EUR',1,'manual')"
    )
    conn.commit()

    async def fake_fetch(symbols):
        assert symbols == ["EURUSD=X"]
        return {"EURUSD=X": 1.1}

    monkeypatch.setattr("app.pricing.fetch_yahoo", fake_fetch)
    asyncio.run(refresh_prices(conn))
    row = conn.execute("SELECT rate,source FROM fx_rates WHERE currency='EUR'").fetchone()
    assert dict(row) == {"rate": 1.1, "source": "yahoo"}
    conn.close()


def test_base_currency_change_clears_rates(fx_client):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO fx_rates(currency,rate,ts,source) VALUES ('EUR',1.2,'now','manual')"
    )
    conn.commit()
    conn.close()
    response = fx_client.post(
        "/settings",
        data={
            "fund_name": "Ledger",
            "leverage": "1",
            "borrow_rate": "0.05",
            "snapshot_enabled": "1",
            "benchmark_symbol": "SPY",
            "base_currency": "EUR",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Base%20currency%20changed%3B" in response.headers["location"]
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0
    assert db.get_setting(conn, "base_currency") == "EUR"
    conn.close()


def test_lookup_returns_fx_rate(fx_client, monkeypatch):
    async def fake_meta(symbol):
        return {
            "symbol": symbol,
            "name": "Euro asset",
            "asset_class": "equity",
            "currency": "EUR",
            "price": 50,
        }

    async def fake_rates(currencies, base):
        assert currencies == ["EUR"]
        assert base == "USD"
        return {"EUR": 1.2}

    monkeypatch.setattr("app.routes.api.fetch_yahoo_meta", fake_meta)
    monkeypatch.setattr("app.routes.api.fetch_fx_rates", fake_rates)
    response = fx_client.get("/api/lookup?symbol=E")
    assert response.status_code == 200
    assert response.json()["fx_rate"] == 1.2


def test_base_eur_uses_inverse_usd_quote(monkeypatch):
    async def fake_fetch(symbols):
        if symbols == ["USDEUR=X"]:
            return {"USDEUR=X": None}
        assert symbols == ["EURUSD=X"]
        return {"EURUSD=X": 0.9}

    monkeypatch.setattr("app.pricing.fetch_yahoo", fake_fetch)
    assert asyncio.run(fetch_fx_rates(["USD"], "EUR"))["USD"] == pytest.approx(1 / 0.9)


def test_fx_columns_migrate_on_old_schema(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE trades (
          id INTEGER PRIMARY KEY,
          instrument_id INTEGER NOT NULL,
          ts TEXT NOT NULL,
          side TEXT NOT NULL,
          quantity REAL NOT NULL,
          price REAL NOT NULL,
          fees REAL NOT NULL DEFAULT 0,
          notes TEXT
        );
        CREATE TABLE snapshot_marks (
          date TEXT NOT NULL,
          instrument_id INTEGER NOT NULL,
          mark REAL NOT NULL,
          source TEXT NOT NULL,
          PRIMARY KEY (date, instrument_id)
        );
        INSERT INTO trades(instrument_id,ts,side,quantity,price)
        VALUES (1,'2026-01-01','BUY',1,10);
        """
    )
    db.init_db(conn)
    trade_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    mark_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(snapshot_marks)")
    }
    assert "fx_rate" in trade_columns
    assert "fx_rate" in mark_columns
    assert conn.execute("SELECT fx_rate FROM trades").fetchone()["fx_rate"] == 1
    conn.close()
