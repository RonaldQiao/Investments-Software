import pytest

from app.benchmark import beta, pair_returns, yahoo_benchmark_symbol
from app.db import init_db, set_setting
from app.nav import history_series, take_snapshot


def test_yahoo_benchmark_symbol_aliases():
    assert yahoo_benchmark_symbol("SPX") == "^GSPC"
    assert yahoo_benchmark_symbol("$SPX") == "^GSPC"
    assert yahoo_benchmark_symbol("^SPX") == "^GSPC"
    assert yahoo_benchmark_symbol("spy") == "SPY"
    assert yahoo_benchmark_symbol("^GSPC") == "^GSPC"


def test_pair_returns_and_beta():
    fund_rows = [
        {"date": "2026-01-01", "daily_return": None},
        {"date": "2026-01-02", "daily_return": 0.02},
        {"date": "2026-01-03", "daily_return": -0.01},
        {"date": "2026-01-04", "daily_return": 0.04},
    ]
    benchmark_rows = [
        {"date": "2026-01-01", "close": 100},
        {"date": "2026-01-02", "close": 110},
        {"date": "2026-01-03", "close": 100},
        {"date": "2026-01-04", "close": 120},
    ]
    pairs = pair_returns(fund_rows, benchmark_rows)
    assert [fund for fund, _ in pairs] == pytest.approx([0.02, -0.01, 0.04])
    assert [benchmark for _, benchmark in pairs] == pytest.approx(
        [0.1, 100 / 110 - 1, 0.2]
    )
    assert beta(pairs) == pytest.approx(0.16992433795712483)


def test_beta_requires_variation():
    assert beta([(0.1, 0.0), (0.2, 0.0)]) is None


def test_snapshot_benchmark_close_is_date_matched(monkeypatch):
    import sqlite3

    from app import pricing

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    set_setting(conn, "benchmark_symbol", "SPY")

    async def fake_close(symbol, snapshot_date):
        assert symbol == "SPY"
        assert snapshot_date.isoformat() == "2026-01-02"
        return 500.0

    monkeypatch.setattr(pricing, "fetch_benchmark_close", fake_close)
    take_snapshot(conn, __import__("datetime").date(2026, 1, 2), refresh=False)
    row = conn.execute(
        "SELECT close FROM benchmark_closes WHERE symbol='SPY' AND date='2026-01-02'"
    ).fetchone()
    assert row["close"] == 500.0
    assert history_series(conn)["snapshots"][0]["benchmark_return"] is None


def test_history_benchmark_anchored_to_first_snapshot():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    set_setting(conn, "benchmark_symbol", "SPY")
    for day, navpu, ret in (
        ("2022-06-27", 1000, None),
        ("2022-06-28", 1010, 0.01),
        ("2022-06-29", 1020, 0.01),
        ("2022-06-30", 1030, 0.01),
    ):
        conn.execute(
            "INSERT INTO nav_snapshots(date,ts,nav,cash,gross_long,gross_short,"
            "net_exposure,flows_today,units_outstanding,nav_per_unit,daily_return,"
            "levered_return,mgmt_fee_accrued,source) "
            "VALUES (?,?,?,?,0,0,0,0,1,?,?,NULL,0,'imported')",
            (day, "now", navpu, navpu, navpu, ret),
        )
    for day, close in (("2022-06-27", 100), ("2022-06-28", 110), ("2022-06-30", 120)):
        conn.execute(
            "INSERT INTO benchmark_closes(symbol,date,close) VALUES ('SPY',?,?)",
            (day, close),
        )
    conn.commit()
    series = history_series(conn)
    rows = series["snapshots"]
    assert series["summary"]["benchmark_return"] == pytest.approx(0.2)
    assert rows[1]["benchmark_return"] == pytest.approx(0.1)
    assert rows[2]["benchmark_return"] is None
    assert rows[3]["benchmark_return"] == pytest.approx(120 / 110 - 1)
    assert series["benchmark_values"] == pytest.approx([1, 1.1, 1.1, 1.2])


def test_settings_change_backfills_benchmark(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import db

    monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(db, "FUNDS_DIR", tmp_path / "funds")
    conn = db.get_conn()
    init_db(conn)
    conn.execute(
        "INSERT INTO nav_snapshots(date,ts,nav,cash,gross_long,gross_short,"
        "net_exposure,flows_today,units_outstanding,nav_per_unit,daily_return,"
        "levered_return,mgmt_fee_accrued,source) "
        "VALUES ('2022-06-27','now',1000,1000,0,0,0,0,1,1000,NULL,NULL,0,'manual')"
    )
    conn.commit()
    conn.close()

    async def fake_closes(symbol, start, end):
        return {"2022-06-27": 100.0}

    monkeypatch.setattr("app.nav.fetch_benchmark_closes", fake_closes)
    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            data={
                "fund_name": "Ledger",
                "leverage": "1.0",
                "borrow_rate": "0.05",
                "snapshot_enabled": "1",
                "benchmark_symbol": "SPX",
                "base_currency": "USD",
                "inception_nav_per_unit": "1000",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    conn = db.get_conn()
    row = conn.execute(
        "SELECT close FROM benchmark_closes WHERE symbol='SPX' AND date='2022-06-27'"
    ).fetchone()
    conn.close()
    assert row["close"] == 100.0
