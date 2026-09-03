import pytest

from app.benchmark import beta, pair_returns
from app.db import init_db, set_setting
from app.nav import history_series, take_snapshot


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
