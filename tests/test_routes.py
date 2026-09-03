import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
    from app import db

    db.DB_PATH = tmp_path / "ledger.db"
    from app.main import app

    with TestClient(app) as test_client:
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


def test_dashboard_speed_guard(client):
    client.get("/")
    durations = []
    for _ in range(20):
        start = time.perf_counter()
        response = client.get("/")
        durations.append(time.perf_counter() - start)
        assert response.status_code == 200
    assert sum(durations) / len(durations) < 0.025
