import csv
import io
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..db import get_conn
from ..fx import trade_fx_rate
from ..web import flash_redirect, render, row_dicts

router = APIRouter()


def _trade_fx(value: str) -> float | None:
    if not value.strip():
        return None
    rate = float(value)
    if rate <= 0:
        raise ValueError("FX rate must be positive")
    return rate

@router.get("/trades", response_class=HTMLResponse)
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


@router.get("/trades.csv")
def trades_csv():
    conn = get_conn()
    rows = conn.execute(
        "SELECT t.ts,i.symbol,t.side,t.quantity,t.price,t.fees,t.fx_rate,t.notes "
        "FROM trades t JOIN instruments i ON i.id=t.instrument_id ORDER BY t.ts,t.id"
    ).fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ts", "symbol", "side", "quantity", "price", "fees", "fx_rate", "notes"])
    writer.writerows(
        [
            row["ts"],
            row["symbol"],
            row["side"],
            row["quantity"],
            row["price"],
            row["fees"],
            row["fx_rate"],
            row["notes"] or "",
        ]
        for row in rows
    )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


@router.post("/trades")
def add_trade(
    instrument_id: int = Form(...),
    ts: str = Form(""),
    side: str = Form(...),
    quantity: float = Form(...),
    price: float = Form(...),
    fees: float = Form(0),
    fx_rate: str = Form(""),
    notes: str = Form(""),
):
    conn = get_conn()
    try:
        provided_fx = _trade_fx(fx_rate)
        conn.execute(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,fx_rate,notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                instrument_id,
                ts or datetime.now(UTC).isoformat(),
                side,
                quantity,
                price,
                fees,
                trade_fx_rate(conn, instrument_id, provided_fx),
                notes,
            ),
        )
    except (TypeError, ValueError) as exc:
        conn.close()
        return flash_redirect("/trades", "error", str(exc))
    conn.commit()
    conn.close()
    return RedirectResponse("/trades", status_code=303)


@router.post("/trades/import")
async def import_trades(file: UploadFile = File(...)):  # noqa: B008
    conn = get_conn()
    try:
        content = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        expected = {"ts", "symbol", "side", "quantity", "price", "fees", "notes"}
        expected_fx = expected | {"fx_rate"}
        if set(reader.fieldnames or ()) not in (expected, expected_fx):
            raise ValueError("CSV columns must include: " + ", ".join(sorted(expected)))
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
                    trade_fx_rate(
                        conn, instruments[symbol], _trade_fx(row.get("fx_rate") or "")
                    ),
                    row["notes"] or "",
                )
            )
        if unknown:
            raise ValueError("Unknown symbols: " + ", ".join(unknown))
        conn.executemany(
            "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,fx_rate,notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            pending,
        )
        conn.commit()
    except (UnicodeDecodeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        conn.rollback()
        conn.close()
        return flash_redirect("/trades", "error", str(exc))
    conn.close()
    return flash_redirect("/trades", "ok", f"Imported {len(pending)} trades")


@router.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/trades", status_code=303)
