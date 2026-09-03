import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..db import DB_PATH, DEFAULT_FUND, get_conn, get_setting, set_setting
from ..web import flash_redirect, render, row_dicts

router = APIRouter()

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    conn = get_conn()
    settings = {
        key: get_setting(conn, key, default)
        for key, default in (
            ("fund_name", "Ledger"),
            ("leverage", "1.0"),
            ("borrow_rate", "0.05"),
            ("snapshot_enabled", "1"),
            ("benchmark_symbol", "SPY"),
        )
    }
    job_logs = row_dicts(
        conn.execute(
            "SELECT ts,job,status,detail FROM job_log ORDER BY id DESC LIMIT 10"
        )
    )
    conn.close()
    return render(request, "settings.html", settings=settings, job_logs=job_logs)



def backup_database():
    source = get_conn()
    database_file = source.execute("PRAGMA database_list").fetchone()["file"]
    backup_dir = Path(
        os.environ.get("LEDGER_BACKUP_DIR", str(Path(database_file).parent / "backups"))
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    fund_slug = DEFAULT_FUND if Path(database_file) == DB_PATH else Path(database_file).stem
    filename = (
        f"ledger-{fund_slug}-"
        f"{datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d-%H%M%S')}.db"
    )
    target_path = backup_dir / filename
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    backups = sorted(backup_dir.glob("ledger-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old in backups[20:]:
        old.unlink()
    return filename


@router.post("/backup")
def create_backup():
    try:
        filename = backup_database()
    except sqlite3.Error as exc:
        return flash_redirect("/settings", "error", f"Backup failed: {exc}")
    return flash_redirect("/settings", "ok", f"Backup created: {filename}")


@router.post("/settings")
def save_settings(
    fund_name: str = Form("Ledger"),
    leverage: float = Form(1.0),
    borrow_rate: float = Form(0.05),
    snapshot_enabled: str = Form("0"),
    benchmark_symbol: str = Form("SPY"),
):
    conn = get_conn()
    set_setting(conn, "fund_name", fund_name.strip() or "Ledger")
    set_setting(conn, "leverage", min(5.0, max(1.0, leverage)))
    set_setting(conn, "borrow_rate", borrow_rate)
    set_setting(conn, "snapshot_enabled", "1" if snapshot_enabled == "1" else "0")
    set_setting(conn, "benchmark_symbol", benchmark_symbol.strip().upper())
    conn.close()
    return RedirectResponse("/settings", status_code=303)
