import csv
import io
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..db import get_conn
from ..web import flash_redirect, render, row_dicts

router = APIRouter()

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


@router.post("/trades")
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


@router.post("/trades/import")
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


@router.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/trades", status_code=303)
