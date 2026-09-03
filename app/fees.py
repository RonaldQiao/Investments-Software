from __future__ import annotations

from datetime import datetime, timezone

from .db import get_setting
from .nav import compute_portfolio


def record_cash_flow(conn, ts, amount, lp_id, note):
    if isinstance(ts, datetime):
        timestamp = ts.isoformat()
        flow_date = ts.date().isoformat()
    else:
        timestamp = str(ts)
        flow_date = timestamp[:10]
    units = conn.execute(
        "SELECT COALESCE(SUM(units),0) units FROM lp_units"
    ).fetchone()["units"]
    if units:
        nav = compute_portfolio(conn)["nav"] - float(
            get_setting(conn, "fee_liability", 0) or 0
        )
        nav_per_unit = nav / units
    else:
        nav_per_unit = float(get_setting(conn, "inception_nav_per_unit", 1000))
    flow = conn.execute(
        "INSERT INTO cash_flows(ts,amount,lp_id,note) VALUES (?,?,?,?)",
        (timestamp, amount, lp_id, note),
    )
    flow_id = flow.lastrowid
    unit_delta = float(amount) / nav_per_unit if nav_per_unit else 0
    conn.execute(
        "INSERT INTO lp_units(lp_id,ts,units,nav_per_unit,cash_flow_id) VALUES (?,?,?,?,?)",
        (lp_id, timestamp, unit_delta, nav_per_unit, flow_id),
    )
    conn.commit()
    return {"cash_flow_id": flow_id, "units": unit_delta, "nav_per_unit": nav_per_unit, "date": flow_date}
