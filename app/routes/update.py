import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..blotter import parse_blotter
from ..db import get_conn
from ..fx import trade_fx_rate
from ..nav import compute_portfolio, take_snapshot
from ..pricing import refresh_prices
from ..web import flash_redirect, render

router = APIRouter()

@router.get("/update", response_class=HTMLResponse)
def update_page(request: Request):
    conn = get_conn()
    portfolio = compute_portfolio(conn)
    instruments = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM instruments").fetchall()
    }
    conn.close()
    positions = []
    for position in portfolio["positions"]:
        item = dict(position)
        item.update(instruments[position["instrument_id"]])
        positions.append(item)
    return render(request, "update.html", positions=positions)


@router.post("/update/marks")
async def update_marks(request: Request):
    form = await request.form()
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        changed = 0
        for key, value in form.items():
            if not key.startswith("mark_") or key.startswith("mark_date_"):
                continue
            instrument_id = int(key[5:])
            instrument = conn.execute(
                "SELECT pricing_source FROM instruments WHERE id=?", (instrument_id,)
            ).fetchone()
            if not instrument or instrument["pricing_source"] != "manual":
                continue
            mark = float(value)
            mark_date = str(form.get(f"mark_date_{instrument_id}") or "").strip()
            timestamp = (
                f"{mark_date}T12:00:00" if mark_date else datetime.now(UTC).isoformat()
            )
            conn.execute(
                "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
                (mark, timestamp, instrument_id),
            )
            changed += 1
        conn.commit()
    except (TypeError, ValueError, sqlite3.Error) as exc:
        conn.rollback()
        conn.close()
        return flash_redirect("/update", "error", f"Invalid mark: {exc}")
    conn.close()
    return flash_redirect("/update", "ok", f"Updated {changed} manual marks")


@router.post("/update/blotter")
async def update_blotter(request: Request):
    form = await request.form()
    conn = get_conn()
    try:
        pending = parse_blotter(str(form.get("blotter") or ""))
        instruments = {
            row["symbol"]: row["id"]
            for row in conn.execute("SELECT symbol,id FROM instruments").fetchall()
        }
        for trade in pending:
            if trade["symbol"] not in instruments:
                raise ValueError(
                    f"Unknown symbol on line {trade['line']}: {trade['symbol']}"
                )
        timestamp = datetime.now(UTC).isoformat()
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,fx_rate,notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    instruments[trade["symbol"]],
                    timestamp,
                    trade["side"],
                    trade["quantity"],
                    trade["price"],
                    trade["fees"],
                    trade_fx_rate(conn, instruments[trade["symbol"]]),
                    "Daily update blotter",
                )
                for trade in pending
            ],
        )
        conn.commit()
    except (ValueError, TypeError, sqlite3.Error) as exc:
        conn.rollback()
        conn.close()
        return flash_redirect("/update", "error", str(exc))
    conn.close()
    return flash_redirect("/update", "ok", f"Recorded {len(pending)} trades")


@router.post("/prices/refresh")
async def prices_refresh_page(return_to: str = Form("/")):
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM instruments WHERE pricing_source='yahoo'"
    ).fetchone()[0]
    failed = await refresh_prices(conn)
    conn.close()
    if failed:
        return flash_redirect(
            return_to,
            "error",
            f"Refreshed {total - len(failed)} prices, {len(failed)} failed: {', '.join(failed)}",
        )
    return flash_redirect(return_to, "ok", f"Refreshed {total} prices, 0 failed")


@router.post("/snapshots/now")
async def snapshot_now(return_to: str = Form("/history")):
    conn = get_conn()
    await refresh_prices(conn)
    today_ny = datetime.now(ZoneInfo("America/New_York")).date()
    take_snapshot(conn, today_ny, "manual", refresh=False)
    conn.close()
    return flash_redirect(return_to, "ok", f"Snapshot written for {today_ny.isoformat()}")
