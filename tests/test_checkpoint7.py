import pytest
from fastapi.testclient import TestClient

from app.blotter import parse_blotter
from app.pricing import occ_symbol


def test_blotter_parser_accepts_trade_aliases_case_insensitively():
    trades = parse_blotter(
        "buy 100 AAPL @ 189.5\n"
        "SELL 2 ES=F @ 5400 fee 4.5\n"
        "short 50 NVDA @ 120\n"
        "COVER 5 NVDA @ 121"
    )
    assert trades == [
        {"side": "BUY", "quantity": 100.0, "symbol": "AAPL", "price": 189.5, "fees": 0.0, "line": 1},
        {"side": "SELL", "quantity": 2.0, "symbol": "ES=F", "price": 5400.0, "fees": 4.5, "line": 2},
        {"side": "SELL", "quantity": 50.0, "symbol": "NVDA", "price": 120.0, "fees": 0.0, "line": 3},
        {"side": "BUY", "quantity": 5.0, "symbol": "NVDA", "price": 121.0, "fees": 0.0, "line": 4},
    ]


def test_blotter_parser_rejects_malformed_line():
    with pytest.raises(ValueError, match="Malformed blotter line 1"):
        parse_blotter("BUY AAPL @ 100")


def test_occ_symbol():
    assert occ_symbol("AAPL", "2026-12-18", "C", 200) == "AAPL261218C00200000"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
    from app import db

    db.DB_PATH = tmp_path / "ledger.db"
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_update_blotter_creates_two_trades(client):
    for symbol in ("AAPL", "ES=F"):
        response = client.post(
            "/instruments",
            data={
                "symbol": symbol,
                "name": symbol,
                "asset_class": "equity",
                "pricing_source": "manual",
                "manual_mark": "100",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    response = client.post(
        "/update/blotter",
        data={"blotter": "BUY 10 AAPL @ 100\nSELL 2 ES=F @ 100 fee 4.5"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(client.get("/api/trades").json()) == 2
