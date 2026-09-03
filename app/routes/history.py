import csv
import io
from datetime import UTC, datetime

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..db import get_conn
from ..nav import backfill_benchmark, history_csv, history_series, import_track_record
from ..web import flash_redirect, render

router = APIRouter()


def _parse_date(value: str):
    for pattern in ("%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(value.strip(), pattern).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value}")


def _parse_amount(value: str) -> float:
    return float(value.strip().replace("$", "").replace(",", ""))


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    conn = get_conn()
    series = history_series(conn)
    conn.close()
    return render(request, "history.html", **series)


@router.post("/history/import")
async def import_history(file: UploadFile):
    conn = get_conn()
    try:
        content = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames or "date" not in reader.fieldnames or "nav" not in reader.fieldnames:
            raise ValueError("CSV must include date and nav columns")
        rows = []
        for row in reader:
            if not row.get("date", "").strip():
                raise ValueError("CSV date is required")
            rows.append(
                {
                    "date": _parse_date(row["date"]),
                    "nav": _parse_amount(row["nav"]),
                    "flow": _parse_amount(row.get("flow", "") or "0"),
                }
            )
        result = import_track_record(conn, rows)
        closes = await backfill_benchmark(conn, [row["date"] for row in rows])
        symbol = str(
            conn.execute(
                "SELECT value FROM settings WHERE key='benchmark_symbol'"
            ).fetchone()["value"]
        ).strip().upper()
        message = (
            f"Imported {result['imported']} points {result['first']} → {result['last']}; "
            f"{symbol or 'benchmark'} closes: {closes}"
        )
        return flash_redirect("/history", "ok", message)
    except (UnicodeDecodeError, ValueError, csv.Error, TypeError, AttributeError) as exc:
        return flash_redirect("/history", "error", str(exc))
    finally:
        conn.close()


@router.post("/history/import/clear")
def clear_imported_history():
    conn = get_conn()
    conn.execute("DELETE FROM nav_snapshots WHERE source='imported'")
    has_live = conn.execute(
        "SELECT 1 FROM nav_snapshots WHERE source!='imported' LIMIT 1"
    ).fetchone()
    if not has_live:
        conn.execute(
            "UPDATE settings SET value='1000' WHERE key='inception_nav_per_unit'"
        )
    conn.commit()
    conn.close()
    return RedirectResponse("/history", status_code=303)


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
