from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db
from app.db import get_setting, init_db, set_setting
from app.nav import history_series, import_track_record, take_snapshot


def database(tmp_path=None, monkeypatch=None):
    if tmp_path is None:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = __import__("sqlite3").Row
    else:
        monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "ledger.db")
        monkeypatch.setattr(db, "FUNDS_DIR", tmp_path / "funds")
        conn = db.get_conn()
    init_db(conn)
    set_setting(conn, "benchmark_symbol", "")
    return conn


def test_import_chain_math_and_inception(tmp_path, monkeypatch):
    conn = database(tmp_path, monkeypatch)
    result = import_track_record(
        conn,
        [
            {"date": date(2022, 6, 27), "nav": 15000, "flow": 0},
            {"date": date(2023, 6, 27), "nav": 21149.32, "flow": 2676.48},
            {"date": date(2026, 9, 3), "nav": 39723.94, "flow": 0},
        ],
    )
    rows = conn.execute(
        "SELECT * FROM nav_snapshots WHERE source='imported' ORDER BY date"
    ).fetchall()
    assert rows[1]["daily_return"] == pytest.approx(0.2315, abs=0.0001)
    assert rows[2]["daily_return"] == pytest.approx(0.8783, abs=0.0001)
    assert float(get_setting(conn, "inception_nav_per_unit")) == pytest.approx(
        rows[-1]["nav_per_unit"]
    )
    assert result["continuity"] == "set inception NAV/unit to imported endpoint"
    assert history_series(conn)["summary"]["inception_return"] == pytest.approx(1.3131, abs=0.0001)


def test_import_scales_to_first_live_and_rejects_late_dates(tmp_path, monkeypatch):
    conn = database(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO nav_snapshots(date,ts,nav,cash,gross_long,gross_short,net_exposure,"
        "units_outstanding,nav_per_unit,daily_return,source) "
        "VALUES ('2026-09-04','now',1000,1000,0,0,0,1,1000,NULL,'manual')"
    )
    conn.commit()
    result = import_track_record(
        conn,
        [
            {"date": date(2022, 6, 27), "nav": 15000, "flow": 0},
            {"date": date(2023, 6, 27), "nav": 21149.32, "flow": 2676.48},
        ],
    )
    last = conn.execute(
        "SELECT nav_per_unit FROM nav_snapshots WHERE source='imported' ORDER BY date DESC LIMIT 1"
    ).fetchone()["nav_per_unit"]
    assert last == pytest.approx(1000)
    assert result["continuity"] == "scaled to first live snapshot"
    with pytest.raises(ValueError, match="precede"):
        import_track_record(conn, [{"date": date(2026, 9, 4), "nav": 100, "flow": 0}])


def test_reimport_replaces_imported_rows(tmp_path, monkeypatch):
    conn = database(tmp_path, monkeypatch)
    import_track_record(conn, [{"date": date(2022, 1, 1), "nav": 100, "flow": 0}])
    import_track_record(conn, [{"date": date(2023, 1, 1), "nav": 200, "flow": 0}])
    rows = conn.execute(
        "SELECT date FROM nav_snapshots WHERE source='imported'"
    ).fetchall()
    assert [row["date"] for row in rows] == ["2023-01-01"]


def test_import_route_parses_csv_and_backfills_weekend(tmp_path, monkeypatch):
    conn = database(tmp_path, monkeypatch)
    set_setting(conn, "benchmark_symbol", "SPY")
    conn.close()
    from app.main import app

    async def fake_closes(symbol, start, end):
        return {
            "2022-06-27": 3900.12,
            "2026-09-03": 4328.82,
        }

    monkeypatch.setattr("app.nav.fetch_benchmark_closes", fake_closes)
    with TestClient(app) as client:
        response = client.post(
            "/history/import",
            files={
                "file": (
                    "tr.csv",
                    b'date,nav,flow\n6/27/22,15000,\n7/2/22,"$21,149.32",\n9/3/26,39723.94,0\n',
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    conn = db.get_conn()
    closes = conn.execute(
        "SELECT date,close FROM benchmark_closes WHERE symbol='SPY' ORDER BY date"
    ).fetchall()
    assert [(row["date"], row["close"]) for row in closes] == [
        ("2022-06-27", 3900.12),
        ("2022-07-02", 3900.12),
        ("2026-09-03", 4328.82),
    ]
    series = history_series(conn)
    assert series["summary"]["benchmark_return"] == pytest.approx(4328.82 / 3900.12 - 1)
    empty = database()
    set_setting(empty, "benchmark_symbol", "SPY")
    empty.execute(
        "INSERT INTO benchmark_closes(symbol,date,close) VALUES ('SPY','2022-06-27',3900.12)"
    )
    empty.execute(
        "INSERT INTO benchmark_closes(symbol,date,close) VALUES ('SPY','2022-06-28',3901.12)"
    )
    empty.commit()
    assert history_series(empty)["summary"]["benchmark_return"] is None


def test_first_live_snapshot_after_import_skips_fees(tmp_path, monkeypatch):
    conn = database(tmp_path, monkeypatch)
    result = import_track_record(
        conn, [{"date": date(2026, 9, 2), "nav": 1000, "flow": 0}]
    )
    lp_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
    conn.execute(
        "INSERT INTO cash_flows(ts,amount,lp_id) VALUES ('2026-09-03',1000,?)",
        (lp_id,),
    )
    conn.commit()
    snapshot = take_snapshot(
        conn, date(2026, 9, 3), refresh=False, fetch_benchmark=False
    )
    assert snapshot["mgmt_fee_accrued"] == 0
    assert snapshot["nav_per_unit"] == pytest.approx(result["navpus"][-1])
