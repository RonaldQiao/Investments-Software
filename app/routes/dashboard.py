from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..db import get_conn, get_setting
from ..nav import compute_portfolio, exposure_by_class
from ..scheduler import next_snapshot_label
from ..web import render

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
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
