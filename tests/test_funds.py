import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app.db import create_fund, init_db, set_setting, slugify
from app.scheduler import run_snapshot_job

ROOT = Path(__file__).resolve().parent.parent


def configure_funds(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_NO_SCHEDULER", "1")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(db, "FUNDS_DIR", tmp_path / "funds")
    init_db()
    from app.main import app

    return app


def test_create_fund_and_slug_collisions(tmp_path, monkeypatch):
    configure_funds(tmp_path, monkeypatch)
    slug = create_fund("Fund II")
    assert slug == "fund-ii"
    assert (tmp_path / "funds" / "fund-ii.db").exists()
    conn = db.get_conn(tmp_path / "funds" / "fund-ii.db")
    assert db.get_setting(conn, "fund_name") == "Fund II"
    conn.close()
    try:
        slugify("Fund II")
    except ValueError:
        pass
    else:
        raise AssertionError("expected fund collision")


def test_fund_cookie_isolates_data_and_invalid_cookie_falls_back(tmp_path, monkeypatch):
    app = configure_funds(tmp_path, monkeypatch)
    create_fund("Fund B")
    with TestClient(app) as client:
        response = client.post(
            "/funds/switch",
            data={"fund": "fund-b"},
            headers={"referer": "/positions"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        response = client.post(
            "/funds/switch",
            data={"fund": "fund-b"},
            headers={"referer": "http://testserver/settings?ok=Fund%20created"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/settings"
        client.cookies.set("fund", "fund-b")
        response = client.post(
            "/instruments",
            data={
                "symbol": "FUND-B",
                "name": "Fund B instrument",
                "asset_class": "equity",
                "pricing_source": "manual",
                "manual_mark": "10",
                "quantity": "1",
                "avg_price": "10",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert any(
            row["symbol"] == "FUND-B" for row in client.get("/api/instruments").json()
        )
        client.cookies.set("fund", "ledger")
        assert not any(
            row["symbol"] == "FUND-B" for row in client.get("/api/instruments").json()
        )
        client.cookies.set("fund", "invalid")
        assert not any(
            row["symbol"] == "FUND-B" for row in client.get("/api/instruments").json()
        )


def test_scheduler_writes_snapshot_for_each_fund(tmp_path, monkeypatch):
    configure_funds(tmp_path, monkeypatch)
    create_fund("Fund B")
    for fund in db.list_funds():
        conn = db.get_conn(fund["path"])
        set_setting(conn, "benchmark_symbol", "")
        conn.execute(
            "INSERT INTO cash_flows(ts,amount,note) VALUES ('2024-01-02',1000,'test')"
        )
        conn.commit()
        conn.close()

    async def no_refresh(conn, sleep=asyncio.sleep):
        return []

    monkeypatch.setattr("app.scheduler.refresh_with_retries", no_refresh)
    for fund in db.list_funds():
        conn = db.get_conn(fund["path"])
        result = asyncio.run(
            run_snapshot_job(conn, "scheduled", snapshot_date=date(2024, 1, 2))
        )
        conn.close()
        assert result["snapshot"]["date"] == "2024-01-02"

    for fund in db.list_funds():
        conn = db.get_conn(fund["path"])
        assert conn.execute("SELECT COUNT(*) FROM nav_snapshots").fetchone()[0] == 1
        conn.close()


def test_non_default_backup_uses_shared_backup_directory(tmp_path, monkeypatch):
    configure_funds(tmp_path, monkeypatch)
    slug = create_fund("Fund B")
    token = db.ACTIVE_DB.set(db.fund_path(slug))
    try:
        from app.main import backup_database

        filename = backup_database()
    finally:
        db.ACTIVE_DB.reset(token)
    assert filename.startswith("ledger-fund-b-")
    assert (tmp_path / "backups" / filename).exists()


def test_snapshot_cli_selects_one_fund(tmp_path, monkeypatch):
    configure_funds(tmp_path, monkeypatch)
    create_fund("Fund B")
    for fund in db.list_funds():
        conn = db.get_conn(fund["path"])
        set_setting(conn, "benchmark_symbol", "")
        conn.execute(
            "INSERT INTO cash_flows(ts,amount,note) VALUES ('2024-01-02',1000,'test')"
        )
        conn.commit()
        conn.close()
    env = os.environ | {
        "LEDGER_DB": str(tmp_path / "ledger.db"),
        "LEDGER_NO_SCHEDULER": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.snapshot", "--fund", "fund-b", "--date", "2024-01-02"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("snapshot 2024-01-02")
    default = sqlite3.connect(tmp_path / "ledger.db")
    assert default.execute("SELECT COUNT(*) FROM nav_snapshots").fetchone()[0] == 0
    default.close()
