import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..db import get_conn, get_setting, set_setting
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
            ("base_currency", "USD"),
        )
    }
    job_logs = row_dicts(
        conn.execute(
            "SELECT ts,job,status,detail FROM job_log ORDER BY id DESC LIMIT 10"
        )
    )
    base_currency = str(settings["base_currency"]).upper()
    fx_currencies = row_dicts(
        conn.execute(
            "SELECT i.currency,fx.rate,fx.ts,fx.source "
            "FROM instruments i LEFT JOIN fx_rates fx "
            "ON fx.currency=UPPER(i.currency) "
            "GROUP BY UPPER(i.currency) ORDER BY UPPER(i.currency)"
        )
    )
    for row in fx_currencies:
        if row["currency"].upper() == base_currency:
            row.update(rate=1.0, ts=None, source="base")
    conn.close()
    return render(
        request,
        "settings.html",
        settings=settings,
        job_logs=job_logs,
        fx_currencies=fx_currencies,
    )



def backup_database():
    source = get_conn()
    database_file = source.execute("PRAGMA database_list").fetchone()["file"]
    backup_dir = Path(
        os.environ.get("LEDGER_BACKUP_DIR", str(db.DB_PATH.parent / "backups"))
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    fund_slug = db.DEFAULT_FUND if Path(database_file) == db.DB_PATH else Path(database_file).stem
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
    base_currency: str = Form("USD"),
):
    base_currency = base_currency.strip().upper()
    if len(base_currency) != 3 or not base_currency.isalpha():
        return flash_redirect("/settings", "error", "Base currency must be 3 letters")
    conn = get_conn()
    previous_base = str(get_setting(conn, "base_currency", "USD")).strip().upper()
    base_changed = base_currency != previous_base
    if base_changed:
        conn.execute("DELETE FROM fx_rates")
    set_setting(conn, "fund_name", fund_name.strip() or "Ledger")
    set_setting(conn, "leverage", min(5.0, max(1.0, leverage)))
    set_setting(conn, "borrow_rate", borrow_rate)
    set_setting(conn, "snapshot_enabled", "1" if snapshot_enabled == "1" else "0")
    set_setting(conn, "benchmark_symbol", benchmark_symbol.strip().upper())
    set_setting(conn, "base_currency", base_currency)
    conn.close()
    if base_changed:
        return flash_redirect(
            "/settings",
            "ok",
            "Base currency changed; FX rates will refetch on next price refresh",
        )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/fx")
def save_fx_rate(currency: str = Form(...), rate: float = Form(...)):
    currency = currency.strip().upper()
    conn = get_conn()
    base = str(get_setting(conn, "base_currency", "USD")).upper()
    if len(currency) != 3 or not currency.isalpha() or currency == base or rate <= 0:
        conn.close()
        return flash_redirect("/settings", "error", "Invalid FX rate")
    conn.execute(
        "INSERT INTO fx_rates(currency,rate,ts,source) VALUES (?,?,?,'manual') "
        "ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate,ts=excluded.ts,"
        "source='manual'",
        (currency, rate, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/fx/clear")
async def clear_fx_rate(currency: str = Form(...)):
    from ..pricing import refresh_fx_rate

    conn = get_conn()
    currency = currency.strip().upper()
    conn.execute("DELETE FROM fx_rates WHERE currency=?", (currency,))
    conn.commit()
    await refresh_fx_rate(conn, currency)
    conn.close()
    return RedirectResponse("/settings", status_code=303)
