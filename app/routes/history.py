from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..db import get_conn
from ..nav import history_csv, history_series
from ..web import render

router = APIRouter()

@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    conn = get_conn()
    series = history_series(conn)
    conn.close()
    return render(request, "history.html", **series)


@router.post("/snapshots/{snapshot_date}/delete")
def delete_snapshot(snapshot_date: str):
    conn = get_conn()
    conn.execute("DELETE FROM nav_snapshots WHERE date=?", (snapshot_date,))
    conn.commit()
    conn.close()
    return RedirectResponse("/history", status_code=303)


@router.get("/history.csv")
def history_csv_download():
    conn = get_conn()
    content = history_csv(conn)
    conn.close()
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=nav_history.csv"},
    )
