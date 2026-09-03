from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .attribution import attribution
from .blotter import parse_blotter
from .db import get_conn, get_setting, init_db, set_setting
from .fees import (
    crystallize_perf_fee,
    fee_scenario,
    ownership,
    record_cash_flow,
    settle_fees,
)
from .nav import (
    compute_portfolio,
    exposure_by_class,
    history_csv,
    history_series,
    take_snapshot,
)
from .positions import build_positions
from .pricing import refresh_prices
from .scheduler import (
    catch_up_async,
    next_snapshot_label,
)
from .scheduler import (
    start as start_scheduler,
)
from .scheduler import (
    stop as stop_scheduler,
)

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")
app = FastAPI(title="Ledger")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.on_event("startup")
async def startup():
    init_db()
    if os.environ.get("LEDGER_NO_SCHEDULER") != "1":
        await catch_up_async()
        start_scheduler()


@app.on_event("shutdown")
def shutdown():
    stop_scheduler()


def context(request: Request, **kwargs):
    conn = get_conn()
    kwargs.setdefault("fund_name", get_setting(conn, "fund_name", "Ledger"))
    conn.close()
    if kwargs.get("error") is None:
        kwargs["error"] = request.query_params.get("error")
    if kwargs.get("ok") is None:
        kwargs["ok"] = request.query_params.get("ok")
    return {"request": request, **kwargs}


def row_dicts(rows):
    return [dict(row) for row in rows]


def number(value, decimals=2):
    value = float(value or 0)
    sign = "−" if value < 0 else ""
    return f"{sign}{abs(value):,.{decimals}f}"


def percent(value):
    if value is None:
        return "—"
    value = float(value)
    return f"{'−' if value < 0 else '+'}{abs(value) * 100:.2f}%"


def fee_bps(value):
    return "—" if value is None or value == "—" else f"{float(value):.0f}"


def fee_pct(value):
    return "—" if value is None or value == "—" else f"{float(value):g}"


def signed_number(value):
    if value is None:
        return "—"
    value = float(value)
    if round(value, 2) == 0:
        return "0.00"
    return f"{value:+.2f}"


def age(value):
    if value is None:
        return ""
    seconds = int(value)
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    return f"{max(1, seconds // 3600)}h"


def expiry_days(value):
    if not value:
        return None
    try:
        return (date.fromisoformat(str(value)) - datetime.now(ZoneInfo("America/New_York")).date()).days
    except ValueError:
        return None


def flash_redirect(path, key, message):
    return RedirectResponse(f"{path}?{key}={quote(str(message))}", status_code=303)


templates.env.filters["number"] = number
templates.env.filters["percent"] = percent
templates.env.filters["fee_bps"] = fee_bps
templates.env.filters["fee_pct"] = fee_pct
templates.env.filters["signed_number"] = signed_number
templates.env.filters["age"] = age
templates.env.filters["expiry_days"] = expiry_days


def render(request: Request, name: str, status_code: int = 200, **kwargs):
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context(request, **kwargs),
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = get_conn()
    portfolio = compute_portfolio(conn)
    latest = get_setting(conn, "last_refresh_at", "") or conn.execute(
        "SELECT MAX(ts) ts FROM prices"
    ).fetchone()["ts"]
    fee_liability = float(get_setting(conn, "fee_liability", 0) or 0)
    snapshot = conn.execute(
        "SELECT date,nav_per_unit,daily_return FROM nav_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()
    next_snapshot = next_snapshot_label(conn)
    conn.close()
    portfolio["fees_accrued"] = fee_liability
    return render(
        request,
        "dashboard.html",
        portfolio=portfolio,
        latest_refresh=latest,
        latest_snapshot=snapshot,
        exposure=exposure_by_class(portfolio),
        next_snapshot=next_snapshot,
    )


@app.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request):
    conn = get_conn()
    instruments = row_dicts(conn.execute("SELECT * FROM instruments ORDER BY symbol"))
    portfolio = compute_portfolio(conn)
    conn.close()
    by_id = {item["instrument_id"]: item for item in portfolio["positions"]}
    return render(request, "positions.html", instruments=instruments, positions=by_id)


@app.get("/update", response_class=HTMLResponse)
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


@app.post("/update/marks")
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


@app.post("/update/blotter")
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
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    instruments[trade["symbol"]],
                    timestamp,
                    trade["side"],
                    trade["quantity"],
                    trade["price"],
                    trade["fees"],
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


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request):
    conn = get_conn()
    instruments = row_dicts(conn.execute("SELECT id,symbol FROM instruments ORDER BY symbol"))
    trades = row_dicts(
        conn.execute(
            "SELECT t.*, i.symbol FROM trades t JOIN instruments i ON i.id=t.instrument_id "
            "ORDER BY t.ts DESC,t.id DESC"
        )
    )
    conn.close()
    return render(request, "trades.html", instruments=instruments, trades=trades)


@app.get("/trades.csv")
def trades_csv():
    conn = get_conn()
    rows = conn.execute(
        "SELECT t.ts,i.symbol,t.side,t.quantity,t.price,t.fees,t.notes "
        "FROM trades t JOIN instruments i ON i.id=t.instrument_id ORDER BY t.ts,t.id"
    ).fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ts", "symbol", "side", "quantity", "price", "fees", "notes"])
    writer.writerows(
        [row["ts"], row["symbol"], row["side"], row["quantity"], row["price"], row["fees"], row["notes"] or ""]
        for row in rows
    )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


def pnl_context(conn, days="30"):
    latest = conn.execute(
        "SELECT MAX(date) date FROM nav_snapshots"
    ).fetchone()["date"]
    if not latest:
        return {"attribution": {"rows": [], "classes": [], "total": {"pnl": 0.0, "pct_period": 0.0}, "snapshots": []}, "days": days}
    end_date = date.fromisoformat(latest)
    if days == "all":
        start_date = date.min
    else:
        try:
            start_date = end_date.fromordinal(end_date.toordinal() - int(days))
        except (TypeError, ValueError):
            days = "30"
            start_date = end_date.fromordinal(end_date.toordinal() - 30)
    return {
        "attribution": attribution(conn, start_date, end_date),
        "days": days,
        "start_date": start_date,
        "end_date": end_date,
    }


@app.get("/pnl", response_class=HTMLResponse)
def pnl_page(request: Request, days: str = "30"):
    conn = get_conn()
    data = pnl_context(conn, days)
    conn.close()
    return render(request, "pnl.html", **data)


@app.get("/pnl.csv")
def pnl_csv(days: str = "30"):
    conn = get_conn()
    data = pnl_context(conn, days)["attribution"]
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["instrument", "class", "days_held", "pnl", "pct_period", "best_day", "worst_day"])
    for row in data["rows"]:
        writer.writerow(
            [
                row["symbol"],
                row["asset_class"],
                row["days_held"],
                row["pnl"],
                row["pct_period"],
                row["best_day"]["pnl"] if row["best_day"] else "",
                row["worst_day"]["pnl"] if row["worst_day"] else "",
            ]
        )
    writer.writerow(["Total", "", "", data["total"]["pnl"], data["total"]["pct_period"], "", ""])
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pnl.csv"},
    )
@app.get("/settings", response_class=HTMLResponse)
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
    filename = f"ledger-{datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d-%H%M%S')}.db"
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


@app.post("/backup")
def create_backup():
    try:
        filename = backup_database()
    except sqlite3.Error as exc:
        return flash_redirect("/settings", "error", f"Backup failed: {exc}")
    return flash_redirect("/settings", "ok", f"Backup created: {filename}")


def capital_context(conn):
    flows = row_dicts(
        conn.execute(
            "SELECT c.*, l.name lp_name, u.units, u.nav_per_unit "
            "FROM cash_flows c LEFT JOIN lps l ON l.id=c.lp_id "
            "LEFT JOIN lp_units u ON u.cash_flow_id=c.id "
            "ORDER BY c.ts DESC,c.id DESC"
        )
    )
    for flow in flows:
        flow["date"] = flow["ts"][:10]
    lps = row_dicts(conn.execute("SELECT * FROM lps ORDER BY name"))
    totals = {
        "contributed": float(
            conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END),0) FROM cash_flows"
            ).fetchone()[0]
        ),
        "withdrawn": float(
            conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END),0) FROM cash_flows"
            ).fetchone()[0]
        ),
    }
    totals["net_capital"] = totals["contributed"] - totals["withdrawn"]
    totals["units"] = float(
        conn.execute("SELECT COALESCE(SUM(units),0) FROM lp_units").fetchone()[0]
    )
    totals["nav_per_unit"] = ownership(conn)["nav_per_unit"]
    return {"flows": flows, "lps": lps, "lp_ownership": ownership(conn)["rows"], "totals": totals}


@app.get("/capital", response_class=HTMLResponse)
def capital_page(request: Request, error: str | None = None):
    conn = get_conn()
    data = capital_context(conn)
    conn.close()
    return render(
        request,
        "capital.html",
        error=error,
        today=datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        **data,
    )


def lp_statement_context(conn, lp_id: int):
    lp = conn.execute("SELECT * FROM lps WHERE id=?", (lp_id,)).fetchone()
    if not lp:
        return None
    owner = next((row for row in ownership(conn)["rows"] if row["id"] == lp_id), None)
    latest = conn.execute("SELECT MAX(date) date FROM nav_snapshots").fetchone()["date"]
    flows = row_dicts(
        conn.execute(
            "SELECT ts,amount,note FROM cash_flows WHERE lp_id=? ORDER BY ts DESC,id DESC",
            (lp_id,),
        )
    )
    net = owner["contributed"] - owner["withdrawn"]
    owner["fees_paid"] = owner["mgmt_fees"] + owner["perf_fees"]
    return {
        "lp": dict(lp),
        "as_of": latest or datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        "owner": owner,
        "flows": flows,
        "net_contributions": net,
        "simple_return": owner["value"] / net - 1 if net else None,
    }


@app.get("/lps/{lp_id}/statement", response_class=HTMLResponse)
def lp_statement(request: Request, lp_id: int):
    conn = get_conn()
    data = lp_statement_context(conn, lp_id)
    conn.close()
    if not data:
        return flash_redirect("/capital", "error", "LP not found")
    return render(request, "statement.html", **data)


@app.get("/lps/{lp_id}/statement.csv")
def lp_statement_csv(lp_id: int):
    conn = get_conn()
    data = lp_statement_context(conn, lp_id)
    conn.close()
    if not data:
        return Response("LP not found\n", status_code=404, media_type="text/plain")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "LP",
            "As of",
            "Units",
            "NAV/unit",
            "Value",
            "% ownership",
            "Contributed",
            "Withdrawn",
            "Net contributions",
            "Simple return",
            "Management bps",
            "Performance %",
            "HWM NAV/unit",
            "Fees paid",
        ]
    )
    owner = data["owner"]
    writer.writerow(
        [
            data["lp"]["name"],
            data["as_of"],
            owner["units"],
            owner["value"] / owner["units"] if owner["units"] else 0,
            owner["value"],
            owner["percentage"],
            owner["contributed"],
            owner["withdrawn"],
            data["net_contributions"],
            data["simple_return"] if data["simple_return"] is not None else "",
            owner["mgmt_fee_bps"],
            owner["perf_fee_pct"],
            owner["hwm"],
            owner["fees_paid"],
        ]
    )
    writer.writerow([])
    writer.writerow(["Date", "Amount", "Note"])
    for flow in data["flows"]:
        writer.writerow([flow["ts"], flow["amount"], flow["note"] or ""])
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={data['lp']['name']}-statement.csv"},
    )


@app.post("/capital")
def add_capital_flow(
    request: Request,
    flow_date: str = Form(""),
    lp_id: str = Form(""),
    new_lp: str = Form(""),
    flow_type: str = Form("Contribution"),
    amount: float = Form(...),
    note: str = Form(""),
    new_mgmt_fee_bps: str = Form(""),
    new_perf_fee_pct: str = Form(""),
):
    conn = get_conn()
    try:
        if lp_id == "new":
            name = new_lp.strip()
            if not name:
                raise ValueError("Enter a name for the new LP")
            mgmt = float(new_mgmt_fee_bps) if new_mgmt_fee_bps.strip() else None
            perf = float(new_perf_fee_pct) if new_perf_fee_pct.strip() else None
            cursor = conn.execute(
                "INSERT INTO lps(name,is_gp,mgmt_fee_bps,perf_fee_pct) VALUES (?,0,?,?)",
                (name, mgmt, perf),
            )
            selected_lp = cursor.lastrowid
        else:
            selected_lp = int(lp_id)
        if amount <= 0:
            raise ValueError("Amount must be greater than zero")
        timestamp = flow_date.strip() or datetime.now(UTC).date().isoformat()
        signed_amount = amount if flow_type == "Contribution" else -amount
        record_cash_flow(conn, timestamp, signed_amount, selected_lp, note.strip())
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        conn.close()
        return flash_redirect("/capital", "error", str(exc))
    conn.close()
    return RedirectResponse("/capital", status_code=303)


@app.post("/capital/{flow_id}/delete")
def delete_capital_flow(flow_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM lp_units WHERE cash_flow_id=?", (flow_id,))
    conn.execute("DELETE FROM cash_flows WHERE id=?", (flow_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/capital", status_code=303)


def fees_context(conn, request: Request):
    calc = None
    params = request.query_params
    if params.get("starting_nav"):
        try:
            calc = fee_scenario(
                params["starting_nav"],
                params.get("starting_nav_per_unit", 1000),
                params.get("hwm_per_unit", params.get("hwm")),
                params.get("gross_return_pct", params.get("gross_return", 0)),
                params.get("mgmt_pct", params.get("mgmt", 2)),
                params.get("perf_pct", params.get("perf", 20)),
            )
        except (TypeError, ValueError):
            calc = None
    settings = {
        key: get_setting(conn, key, default)
        for key, default in (
            ("mgmt_fee_bps", "200"),
            ("perf_fee_pct", "20"),
            ("fee_liability", "0"),
        )
    }
    lp_ownership = ownership(conn)
    calc_lp_rows = []
    if calc:
        npu_after_mgmt = (
            float(calc["net_end_npu"]) + float(calc["perf_fee"]) / (calc["units"] or 1)
        )
        for lp in lp_ownership["rows"]:
            if lp["is_gp"] or not lp["units"]:
                continue
            hwm = lp["hwm"] if lp["hwm"] is not None else float(calc["starting_nav_per_unit"])
            gain = max(0.0, npu_after_mgmt - hwm)
            pct = (
                float(lp["perf_fee_pct"])
                if lp["perf_fee_pct"] is not None
                else float(settings["perf_fee_pct"])
            )
            fee = gain * lp["units"] * pct / 100
            calc_lp_rows.append(
                {
                    "name": lp["name"],
                    "units": lp["units"],
                    "hwm": hwm,
                    "gain": gain,
                    "perf_fee": fee,
                    "new_hwm": npu_after_mgmt if fee else hwm,
                }
            )
    return {
        "settings": settings,
        "lps": row_dicts(conn.execute("SELECT * FROM lps ORDER BY name")),
        "ownership": lp_ownership,
        "mgmt_accrued": float(
            conn.execute("SELECT COALESCE(SUM(amount),0) FROM fee_events WHERE kind='mgmt'").fetchone()[0]
        ),
        "perf_accrued": float(
            conn.execute("SELECT COALESCE(SUM(amount),0) FROM fee_events WHERE kind='perf'").fetchone()[0]
        ),
        "calc": calc,
        "calc_lp_rows": calc_lp_rows,
    }


@app.get("/fees", response_class=HTMLResponse)
def fees_page(request: Request, error: str | None = None):
    conn = get_conn()
    data = fees_context(conn, request)
    conn.close()
    return render(request, "fees.html", error=error, **data)


@app.post("/fees/terms")
def save_fee_terms(
    gp_id: str = Form(""),
    mgmt_fee_bps: float = Form(...),
    perf_fee_pct: float = Form(...),
):
    conn = get_conn()
    set_setting(conn, "mgmt_fee_bps", mgmt_fee_bps)
    set_setting(conn, "perf_fee_pct", perf_fee_pct)
    if gp_id:
        conn.execute("UPDATE lps SET is_gp=0")
        conn.execute("UPDATE lps SET is_gp=1 WHERE id=?", (int(gp_id),))
        conn.commit()
    conn.close()
    return RedirectResponse("/fees", status_code=303)


@app.post("/fees/lps")
def add_lp(
    name: str = Form(...),
    mgmt_fee_bps: str = Form(""),
    perf_fee_pct: str = Form(""),
):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO lps(name,is_gp,mgmt_fee_bps,perf_fee_pct) VALUES (?,?,?,?)",
            (
                name.strip(),
                0,
                float(mgmt_fee_bps) if mgmt_fee_bps.strip() else None,
                float(perf_fee_pct) if perf_fee_pct.strip() else None,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return RedirectResponse("/fees", status_code=303)


@app.post("/fees/crystallize")
def crystallize_fees(request: Request):
    conn = get_conn()
    try:
        crystallize_perf_fee(conn, datetime.now(UTC))
    except ValueError as exc:
        conn.close()
        return flash_redirect("/fees", "error", str(exc))
    conn.close()
    return RedirectResponse("/fees", status_code=303)


@app.post("/fees/settle")
def settle_fee_liability(request: Request):
    conn = get_conn()
    try:
        settle_fees(conn, datetime.now(UTC))
    except ValueError as exc:
        conn.close()
        return flash_redirect("/fees", "error", str(exc))
    conn.close()
    return RedirectResponse("/fees", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    conn = get_conn()
    series = history_series(conn)
    conn.close()
    return render(request, "history.html", **series)


@app.post("/settings")
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


@app.post("/instruments")
def add_instrument(
    symbol: str = Form(...),
    name: str = Form(...),
    asset_class: str = Form(...),
    currency: str = Form("USD"),
    multiplier: float = Form(1),
    pricing_source: str = Form("yahoo"),
    yahoo_symbol: str = Form(""),
    manual_mark: str = Form(""),
    notes: str = Form(""),
    underlying: str = Form(""),
    expiry: str = Form(""),
    strike: str = Form(""),
    option_type: str = Form(""),
):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
            "yahoo_symbol,manual_mark,notes,underlying,expiry,strike,option_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                symbol.strip().upper(),
                name.strip(),
                asset_class,
                currency.strip().upper(),
                multiplier,
                pricing_source,
                yahoo_symbol.strip() or None,
                float(manual_mark) if manual_mark.strip() else None,
                notes.strip(),
                underlying.strip() or None,
                expiry.strip() or None,
                float(strike) if strike.strip() else None,
                option_type.strip().upper() or None,
            ),
        )
        conn.commit()
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        conn.close()
        return flash_redirect("/positions", "error", str(exc))
    conn.close()
    return flash_redirect("/positions", "ok", "Instrument added")


@app.post("/instruments/{instrument_id}/edit")
def edit_instrument(
    instrument_id: int,
    name: str = Form(...),
    asset_class: str = Form(...),
    multiplier: float = Form(...),
    pricing_source: str = Form(...),
    yahoo_symbol: str = Form(""),
    notes: str = Form(""),
    underlying: str = Form(""),
    expiry: str = Form(""),
    strike: str = Form(""),
    option_type: str = Form(""),
):
    conn = get_conn()
    instrument = conn.execute(
        "SELECT * FROM instruments WHERE id=?", (instrument_id,)
    ).fetchone()
    if not instrument:
        conn.close()
        return flash_redirect("/positions", "error", "Instrument not found")
    position = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END),0) "
        "FROM trades WHERE instrument_id=?",
        (instrument_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE instruments SET name=?,asset_class=?,multiplier=?,pricing_source=?,"
        "yahoo_symbol=?,notes=?,underlying=?,expiry=?,strike=?,option_type=? WHERE id=?",
        (
            name.strip(),
            asset_class,
            multiplier,
            pricing_source,
            yahoo_symbol.strip() or None,
            notes.strip(),
            underlying.strip() or None,
            expiry.strip() or None,
            float(strike) if strike.strip() else None,
            option_type.strip().upper() or None,
            instrument_id,
        ),
    )
    conn.commit()
    conn.close()
    message = "Instrument updated"
    if float(multiplier) != float(instrument["multiplier"]) and abs(float(position)) > 1e-12:
        message += "; multiplier changed; NAV recomputed"
    return flash_redirect("/positions", "ok", message)


@app.post("/instruments/{instrument_id}/mark")
def set_mark(instrument_id: int, manual_mark: float = Form(...)):
    conn = get_conn()
    conn.execute(
        "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
        (manual_mark, datetime.now(UTC).isoformat(), instrument_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@app.post("/instruments/{instrument_id}/position")
def set_position(
    instrument_id: int, target_qty: float = Form(...), price: float = Form(...)
):
    conn = get_conn()
    trades = conn.execute(
        "SELECT t.*,i.multiplier FROM trades t JOIN instruments i ON i.id=t.instrument_id "
        "WHERE instrument_id=? ORDER BY ts,id",
        (instrument_id,),
    ).fetchall()
    current = build_positions(trades).get(instrument_id)
    current_qty = current.qty if current else 0
    adjustment = target_qty - current_qty
    if adjustment:
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
            "VALUES (?,?,?,?,?,0,'Set position')",
            (
                instrument_id,
                datetime.now(UTC).isoformat(),
                "BUY" if adjustment > 0 else "SELL",
                abs(adjustment),
                price,
            ),
        )
        conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@app.post("/instruments/{instrument_id}/delete")
def delete_instrument(instrument_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM instruments WHERE id=?", (instrument_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@app.post("/trades")
def add_trade(
    instrument_id: int = Form(...),
    ts: str = Form(""),
    side: str = Form(...),
    quantity: float = Form(...),
    price: float = Form(...),
    fees: float = Form(0),
    notes: str = Form(""),
):
    conn = get_conn()
    conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) VALUES (?,?,?,?,?,?,?)",
        (
            instrument_id,
            ts or datetime.now(UTC).isoformat(),
            side,
            quantity,
            price,
            fees,
            notes,
        ),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/trades", status_code=303)


@app.post("/trades/import")
async def import_trades(file: UploadFile = File(...)):  # noqa: B008
    conn = get_conn()
    try:
        content = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        expected = ["ts", "symbol", "side", "quantity", "price", "fees", "notes"]
        if reader.fieldnames != expected:
            raise ValueError("CSV columns must be: " + ", ".join(expected))
        instruments = {
            row["symbol"]: row["id"]
            for row in conn.execute("SELECT id,symbol FROM instruments").fetchall()
        }
        pending = []
        unknown = []
        for row_number, row in enumerate(reader, start=2):
            symbol = (row.get("symbol") or "").strip().upper()
            if symbol not in instruments:
                unknown.append(f"row {row_number}: {symbol or '(blank)'}")
                continue
            pending.append(
                (
                    instruments[symbol],
                    row["ts"],
                    row["side"].strip().upper(),
                    float(row["quantity"]),
                    float(row["price"]),
                    float(row["fees"] or 0),
                    row["notes"] or "",
                )
            )
        if unknown:
            raise ValueError("Unknown symbols: " + ", ".join(unknown))
        conn.executemany(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
            "VALUES (?,?,?,?,?,?,?)",
            pending,
        )
        conn.commit()
    except (UnicodeDecodeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        conn.rollback()
        conn.close()
        return flash_redirect("/trades", "error", str(exc))
    conn.close()
    return flash_redirect("/trades", "ok", f"Imported {len(pending)} trades")


@app.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/trades", status_code=303)


@app.post("/prices/refresh")
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


@app.post("/snapshots/now")
async def snapshot_now(return_to: str = Form("/history")):
    conn = get_conn()
    await refresh_prices(conn)
    today_ny = datetime.now(ZoneInfo("America/New_York")).date()
    take_snapshot(conn, today_ny, "manual", refresh=False)
    conn.close()
    return flash_redirect(return_to, "ok", f"Snapshot written for {today_ny.isoformat()}")


@app.post("/snapshots/{snapshot_date}/delete")
def delete_snapshot(snapshot_date: str):
    conn = get_conn()
    conn.execute("DELETE FROM nav_snapshots WHERE date=?", (snapshot_date,))
    conn.commit()
    conn.close()
    return RedirectResponse("/history", status_code=303)


@app.get("/api/portfolio")
def portfolio_api():
    conn = get_conn()
    result = compute_portfolio(conn)
    conn.close()
    return result


@app.get("/api/history")
def history_api():
    conn = get_conn()
    result = history_series(conn)
    conn.close()
    return result


@app.get("/history.csv")
def history_csv_download():
    conn = get_conn()
    content = history_csv(conn)
    conn.close()
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=nav_history.csv"},
    )


@app.post("/api/prices/refresh")
async def prices_refresh_api():
    conn = get_conn()
    failed = await refresh_prices(conn)
    result = compute_portfolio(conn)
    conn.close()
    return {"failed": failed, "portfolio": result}


@app.get("/api/instruments")
def instruments_api():
    conn = get_conn()
    rows = row_dicts(conn.execute("SELECT * FROM instruments ORDER BY symbol"))
    conn.close()
    return rows


@app.post("/api/instruments")
async def instruments_create_api(request: Request):
    payload = await request.json()
    conn = get_conn()
    fields = (
        payload.get("symbol", "").strip().upper(),
        payload.get("name", "").strip(),
        payload.get("asset_class", "other"),
        payload.get("currency", "USD"),
        float(payload.get("multiplier", 1)),
        payload.get("pricing_source", "yahoo"),
        payload.get("yahoo_symbol") or None,
        payload.get("manual_mark"),
        payload.get("notes", ""),
        payload.get("underlying") or None,
        payload.get("expiry") or None,
        payload.get("strike"),
        payload.get("option_type") or None,
    )
    cursor = conn.execute(
        "INSERT INTO instruments(symbol,name,asset_class,currency,multiplier,pricing_source,"
        "yahoo_symbol,manual_mark,notes,underlying,expiry,strike,option_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        fields,
    )
    conn.commit()
    row = conn.execute("SELECT * FROM instruments WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


@app.delete("/api/instruments/{instrument_id}")
def instruments_delete_api(instrument_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM instruments WHERE id=?", (instrument_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/instruments/{instrument_id}")
@app.patch("/api/instruments/{instrument_id}")
async def instruments_update_api(instrument_id: int, request: Request):
    payload = await request.json()
    allowed = {
        key: payload[key]
        for key in (
            "symbol",
            "name",
            "asset_class",
            "currency",
            "multiplier",
            "pricing_source",
            "yahoo_symbol",
            "manual_mark",
            "manual_mark_at",
            "notes",
            "underlying",
            "expiry",
            "strike",
            "option_type",
        )
        if key in payload
    }
    if not allowed:
        return JSONResponse({"error": "no fields supplied"}, status_code=400)
    conn = get_conn()
    assignments = ", ".join(f"{key}=?" for key in allowed)
    conn.execute(
        f"UPDATE instruments SET {assignments} WHERE id=?",
        (*allowed.values(), instrument_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM instruments WHERE id=?", (instrument_id,)
    ).fetchone()
    conn.close()
    return JSONResponse(dict(row) if row else {"error": "not found"}, status_code=200 if row else 404)


@app.get("/api/trades")
def trades_api():
    conn = get_conn()
    rows = row_dicts(conn.execute("SELECT * FROM trades ORDER BY ts DESC,id DESC"))
    conn.close()
    return rows


@app.post("/api/trades")
async def trades_create_api(request: Request):
    payload = await request.json()
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) VALUES (?,?,?,?,?,?,?)",
        (
            payload["instrument_id"],
            payload.get("ts") or datetime.now(UTC).isoformat(),
            payload["side"],
            payload["quantity"],
            payload["price"],
            payload.get("fees", 0),
            payload.get("notes", ""),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM trades WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


@app.delete("/api/trades/{trade_id}")
def trades_delete_api(trade_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/trades/{trade_id}")
@app.patch("/api/trades/{trade_id}")
async def trades_update_api(trade_id: int, request: Request):
    payload = await request.json()
    allowed = {
        key: payload[key]
        for key in ("instrument_id", "ts", "side", "quantity", "price", "fees", "notes")
        if key in payload
    }
    if not allowed:
        return JSONResponse({"error": "no fields supplied"}, status_code=400)
    conn = get_conn()
    assignments = ", ".join(f"{key}=?" for key in allowed)
    conn.execute(
        f"UPDATE trades SET {assignments} WHERE id=?",
        (*allowed.values(), trade_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    conn.close()
    return JSONResponse(dict(row) if row else {"error": "not found"}, status_code=200 if row else 404)
