import csv
import io
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..db import get_conn
from ..fees import ownership, record_cash_flow
from ..web import flash_redirect, render, row_dicts

router = APIRouter()


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


@router.get("/capital", response_class=HTMLResponse)
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


@router.get("/lps/{lp_id}/statement", response_class=HTMLResponse)
def lp_statement(request: Request, lp_id: int):
    conn = get_conn()
    data = lp_statement_context(conn, lp_id)
    conn.close()
    if not data:
        return flash_redirect("/capital", "error", "LP not found")
    return render(request, "statement.html", **data)


@router.get("/lps/{lp_id}/statement.csv")
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


@router.post("/capital")
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


@router.post("/capital/{flow_id}/delete")
def delete_capital_flow(flow_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM lp_units WHERE cash_flow_id=?", (flow_id,))
    conn.execute("DELETE FROM cash_flows WHERE id=?", (flow_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/capital", status_code=303)
