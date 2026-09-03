import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..db import get_conn
from ..nav import compute_portfolio
from ..positions import build_positions
from ..pricing import price_instrument
from ..web import flash_redirect, render, row_dicts

router = APIRouter()

@router.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request):
    conn = get_conn()
    instruments = row_dicts(conn.execute("SELECT * FROM instruments ORDER BY symbol"))
    portfolio = compute_portfolio(conn)
    conn.close()
    by_id = {item["instrument_id"]: item for item in portfolio["positions"]}
    return render(request, "positions.html", instruments=instruments, positions=by_id)


@router.post("/instruments")
async def add_instrument(
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
    side: str = Form("LONG"),
    quantity: str = Form(""),
    avg_price: str = Form(""),
    fees: str = Form(""),
):
    side = side.strip().upper()
    try:
        quantity_value = float(quantity) if quantity.strip() else None
        avg_price_value = float(avg_price) if avg_price.strip() else None
        fees_value = float(fees) if fees.strip() else 0
    except (ValueError, TypeError):
        return flash_redirect("/positions", "error", "Invalid opening position")
    if side not in {"LONG", "SHORT"}:
        return flash_redirect("/positions", "error", "Invalid side")
    if quantity_value is not None and quantity_value <= 0:
        return flash_redirect("/positions", "error", "Quantity must be positive")
    if quantity_value is not None and avg_price_value is None:
        return flash_redirect("/positions", "error", "Avg price required with quantity")
    if avg_price_value is not None and avg_price_value < 0:
        return flash_redirect("/positions", "error", "Avg price cannot be negative")
    conn = get_conn()
    try:
        cursor = conn.execute(
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
        if quantity_value is not None:
            conn.execute(
                "INSERT INTO trades(instrument_id,ts,side,quantity,price,fees,notes) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    cursor.lastrowid,
                    datetime.now(UTC).isoformat(),
                    "BUY" if side == "LONG" else "SELL",
                    quantity_value,
                    avg_price_value,
                    fees_value,
                    "Opening position",
                ),
            )
        conn.commit()
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        conn.rollback()
        conn.close()
        return flash_redirect("/positions", "error", str(exc))
    message = "Instrument added with opening position" if quantity_value is not None else "Instrument added"
    if pricing_source == "yahoo":
        price = await price_instrument(conn, cursor.lastrowid)
        message += f" · priced {price:.2f}" if price is not None else "; no Yahoo price yet"
    conn.close()
    return flash_redirect("/positions", "ok", message)


@router.post("/instruments/{instrument_id}/edit")
async def edit_instrument(
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
    new_strike = float(strike) if strike.strip() else None
    changed_for_pricing = (
        pricing_source == "yahoo"
        and (
            instrument["pricing_source"] != pricing_source
            or instrument["yahoo_symbol"] != (yahoo_symbol.strip() or None)
            or instrument["underlying"] != (underlying.strip() or None)
            or instrument["expiry"] != (expiry.strip() or None)
            or instrument["strike"] != new_strike
            or instrument["option_type"] != (option_type.strip().upper() or None)
        )
    )
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
            new_strike,
            option_type.strip().upper() or None,
            instrument_id,
        ),
    )
    conn.commit()
    message = "Instrument updated"
    if float(multiplier) != float(instrument["multiplier"]) and abs(float(position)) > 1e-12:
        message += "; multiplier changed; NAV recomputed"
    if changed_for_pricing:
        price = await price_instrument(conn, instrument_id)
        message += f" · priced {price:.2f}" if price is not None else "; no Yahoo price yet"
    conn.close()
    return flash_redirect("/positions", "ok", message)


@router.post("/instruments/{instrument_id}/mark")
def set_mark(instrument_id: int, manual_mark: float = Form(...)):
    conn = get_conn()
    conn.execute(
        "UPDATE instruments SET manual_mark=?,manual_mark_at=? WHERE id=?",
        (manual_mark, datetime.now(UTC).isoformat(), instrument_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)


@router.post("/instruments/{instrument_id}/position")
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


@router.post("/instruments/{instrument_id}/delete")
def delete_instrument(instrument_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM instruments WHERE id=?", (instrument_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/positions", status_code=303)
