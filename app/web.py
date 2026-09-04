from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import get_conn, get_setting, list_funds

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")


def context(request: Request, **kwargs):
    conn = get_conn()
    kwargs.setdefault("fund_name", get_setting(conn, "fund_name", "Ledger"))
    kwargs.setdefault("base_currency", get_setting(conn, "base_currency", "USD"))
    conn.close()
    funds = list_funds()
    kwargs.setdefault("funds", funds)
    kwargs.setdefault(
        "active_fund",
        next((fund["slug"] for fund in funds if fund["active"]), "ledger"),
    )
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


def input_number(value):
    return "" if value is None else format(float(value), ".12g")


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


def et_timestamp(value):
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
        return parsed.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def flash_redirect(path, key, message):
    return RedirectResponse(f"{path}?{key}={quote(str(message))}", status_code=303)


templates.env.filters["number"] = number
templates.env.filters["input_number"] = input_number
templates.env.filters["percent"] = percent
templates.env.filters["fee_bps"] = fee_bps
templates.env.filters["fee_pct"] = fee_pct
templates.env.filters["signed_number"] = signed_number
templates.env.filters["age"] = age
templates.env.filters["expiry_days"] = expiry_days
templates.env.filters["et_timestamp"] = et_timestamp


def render(request: Request, name: str, status_code: int = 200, **kwargs):
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context(request, **kwargs),
        status_code=status_code,
    )
