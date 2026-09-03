import time

import pytest
from fastapi.testclient import TestClient

from app.pricing import YAHOO_INSTRUMENT_TYPES


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
    from app import db

    db.DB_PATH = tmp_path / "ledger.db"
    from app.main import app

    with TestClient(app) as test_client:
        conn = db.get_conn()
        db.set_setting(conn, "benchmark_symbol", "")
        conn.close()
        yield test_client


def test_pages_and_apis(client):
    for path in (
        "/",
        "/positions",
        "/trades",
        "/settings",
        "/capital",
        "/fees",
        "/history",
        "/pnl",
        "/history.csv",
        "/api/portfolio",
        "/api/history",
    ):
        response = client.get(path)
        assert response.status_code == 200, path


def test_lp_statement_route_and_calculation(client):
    response = client.post(
        "/capital",
        data={"lp_id": "1", "flow_type": "Contribution", "amount": "1000", "flow_date": "2026-01-02"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    statement = client.get("/lps/1/statement")
    assert statement.status_code == 200
    assert "simple return on net contributions" in statement.text.lower()
    assert client.get("/lps/1/statement.csv").status_code == 200


def test_trade_at_mark_is_nav_neutral_and_snapshot_is_written(client):
    before = client.get("/api/portfolio").json()["net_nav"]
    response = client.post(
        "/instruments",
        data={
            "symbol": "TEST",
            "name": "Test instrument",
            "asset_class": "equity",
            "currency": "USD",
            "multiplier": "1",
            "pricing_source": "manual",
            "manual_mark": "10",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    instrument = client.get("/api/instruments").json()[0]
    response = client.post(
        "/trades",
        data={
            "instrument_id": instrument["id"],
            "ts": "2026-01-02T12:00",
            "side": "BUY",
            "quantity": "5",
            "price": "10",
            "fees": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    portfolio = client.get("/api/portfolio").json()
    assert portfolio["net_nav"] == pytest.approx(before)
    assert portfolio["positions"][0]["symbol"] == "TEST"
    response = client.post("/snapshots/now", follow_redirects=False)
    assert response.status_code == 303
    history = client.get("/api/history").json()["snapshots"]
    assert len(history) == 1


def test_add_instrument_with_long_opening_position(client):
    response = client.post(
        "/instruments",
        data={
            "symbol": "LONG",
            "name": "Long instrument",
            "asset_class": "equity",
            "pricing_source": "manual",
            "manual_mark": "189.5",
            "side": "LONG",
            "quantity": "100",
            "avg_price": "189.5",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    instrument = next(row for row in client.get("/api/instruments").json() if row["symbol"] == "LONG")
    trade = client.get("/api/trades").json()[0]
    assert trade["instrument_id"] == instrument["id"]
    assert trade["side"] == "BUY"
    assert trade["quantity"] == 100
    assert trade["price"] == 189.5
    assert trade["notes"] == "Opening position"
    position = client.get("/api/portfolio").json()["positions"][0]
    assert position["side"] == "LONG"
    assert position["qty"] == 100


def test_add_instrument_with_short_opening_position(client):
    response = client.post(
        "/instruments",
        data={
            "symbol": "SHORT",
            "name": "Short instrument",
            "asset_class": "equity",
            "pricing_source": "manual",
            "manual_mark": "120",
            "side": "SHORT",
            "quantity": "50",
            "avg_price": "120",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    trade = client.get("/api/trades").json()[0]
    assert trade["side"] == "SELL"
    assert trade["quantity"] == 50
    position = client.get("/api/portfolio").json()["positions"][0]
    assert position["side"] == "SHORT"
    assert position["qty"] == -50


def test_add_instrument_requires_average_price_for_opening_position(client):
    response = client.post(
        "/instruments",
        data={
            "symbol": "MISSING-AVG",
            "name": "Missing average",
            "asset_class": "equity",
            "quantity": "100",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Avg%20price%20required%20with%20quantity" in response.headers["location"]
    assert not any(row["symbol"] == "MISSING-AVG" for row in client.get("/api/instruments").json())


def test_yahoo_lookup_route(client, monkeypatch):
    async def fake_meta(symbol):
        assert symbol == "NBIS"
        return {
            "symbol": "NBIS",
            "name": "Nebius Group N.V.",
            "asset_class": "equity",
            "currency": "USD",
            "price": 209.2,
        }

    monkeypatch.setattr("app.routes.api.fetch_yahoo_meta", fake_meta)
    response = client.get("/api/lookup?symbol=%20nbis%20")
    assert response.status_code == 200
    assert response.json()["name"] == "Nebius Group N.V."
    assert response.json()["price"] == 209.2

    async def missing_meta(symbol):
        return None

    monkeypatch.setattr("app.routes.api.fetch_yahoo_meta", missing_meta)
    assert client.get("/api/lookup?symbol=UNKNOWN").status_code == 404
    assert client.get("/api/lookup?symbol=%20").status_code == 400


def test_yahoo_instrument_type_mapping():
    assert YAHOO_INSTRUMENT_TYPES["MUTUALFUND"] == "etf"
    assert YAHOO_INSTRUMENT_TYPES["FUTURE"] == "future"
    assert YAHOO_INSTRUMENT_TYPES.get("UNRECOGNIZED", "other") == "other"


def test_yahoo_instrument_is_priced_on_creation(client, monkeypatch):
    async def fake_fetch(symbols):
        assert symbols == ["NBIS"]
        return {"NBIS": 209.2}

    monkeypatch.setattr("app.pricing.fetch_yahoo", fake_fetch)
    response = client.post(
        "/instruments",
        data={
            "symbol": "NBIS",
            "name": "Nebius",
            "asset_class": "equity",
            "quantity": "1000",
            "avg_price": "200.12",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "priced" in response.headers["location"]

    from app import db

    conn = db.get_conn()
    instrument = conn.execute(
        "SELECT id FROM instruments WHERE symbol='NBIS'"
    ).fetchone()
    price = conn.execute(
        "SELECT price FROM prices WHERE instrument_id=?", (instrument["id"],)
    ).fetchone()
    conn.close()
    assert price["price"] == 209.2
    positions = client.get("/api/portfolio").json()["positions"]
    assert positions[0]["mark"] == 209.2
    html = client.get("/positions").text
    assert "209.20" in html
    assert "no price" not in html


def test_yahoo_instrument_creation_without_price_keeps_instrument(client, monkeypatch):
    async def fake_fetch(symbols):
        assert symbols == ["NBIS"]
        return {"NBIS": None}

    monkeypatch.setattr("app.pricing.fetch_yahoo", fake_fetch)
    response = client.post(
        "/instruments",
        data={
            "symbol": "NBIS",
            "name": "Nebius",
            "asset_class": "equity",
            "quantity": "1000",
            "avg_price": "200.12",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "no%20Yahoo%20price%20yet" in response.headers["location"]
    assert any(row["symbol"] == "NBIS" for row in client.get("/api/instruments").json())

    from app import db

    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
    conn.close()


def test_manual_instrument_creation_does_not_fetch_yahoo(client, monkeypatch):
    calls = 0

    async def fake_fetch(symbols):
        nonlocal calls
        calls += 1
        return {"MANUAL": 10}

    monkeypatch.setattr("app.pricing.fetch_yahoo", fake_fetch)
    response = client.post(
        "/instruments",
        data={
            "symbol": "MANUAL",
            "name": "Manual instrument",
            "asset_class": "other",
            "pricing_source": "manual",
            "manual_mark": "10",
            "quantity": "1",
            "avg_price": "10",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == 0


def test_dashboard_speed_guard(client):
    client.get("/")
    durations = []
    for _ in range(20):
        start = time.perf_counter()
        response = client.get("/")
        durations.append(time.perf_counter() - start)
        assert response.status_code == 200
    assert sum(durations) / len(durations) < 0.025
