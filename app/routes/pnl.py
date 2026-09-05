import csv
import io
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from ..attribution import attribution
from ..db import get_conn
from ..web import render

router = APIRouter()


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


@router.get("/pnl", response_class=HTMLResponse)
def pnl_page(request: Request, days: str = "30"):
    conn = get_conn()
    data = pnl_context(conn, days)
    conn.close()
    return render(request, "pnl.html", **data)


@router.get("/pnl.csv")
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
