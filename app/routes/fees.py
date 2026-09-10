import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..db import get_conn, get_setting, set_setting
from ..fees import crystallize_perf_fee, fee_scenario, ownership, settle_fees
from ..web import flash_redirect, render, row_dicts

router = APIRouter()


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


@router.get("/fees", response_class=HTMLResponse)
def fees_page(request: Request, error: str | None = None):
    conn = get_conn()
    data = fees_context(conn, request)
    conn.close()
    return render(request, "fees.html", error=error, **data)


@router.post("/fees/terms")
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


@router.post("/fees/lps")
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


@router.post("/fees/crystallize")
def crystallize_fees(request: Request):
    conn = get_conn()
    try:
        crystallize_perf_fee(conn, datetime.now(UTC))
    except ValueError as exc:
        conn.close()
        return flash_redirect("/fees", "error", str(exc))
    conn.close()
    return RedirectResponse("/fees", status_code=303)


@router.post("/fees/settle")
def settle_fee_liability(request: Request):
    conn = get_conn()
    try:
        settle_fees(conn, datetime.now(UTC))
    except ValueError as exc:
        conn.close()
        return flash_redirect("/fees", "error", str(exc))
    conn.close()
    return RedirectResponse("/fees", status_code=303)
