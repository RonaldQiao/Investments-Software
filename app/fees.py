from __future__ import annotations

from datetime import datetime, timezone

from .db import get_setting, set_setting
from .nav import compute_portfolio


def _timestamp(ts):
    if isinstance(ts, datetime):
        return ts.isoformat(), ts.date().isoformat()
    timestamp = str(ts)
    return timestamp, timestamp[:10]


def current_nav_per_unit(conn):
    units = float(
        conn.execute("SELECT COALESCE(SUM(units), 0) units FROM lp_units").fetchone()[
            "units"
        ]
    )
    liability = float(get_setting(conn, "fee_liability", 0) or 0)
    if units:
        return (float(compute_portfolio(conn)["nav"]) - liability) / units
    snapshot = conn.execute(
        "SELECT nav_per_unit FROM nav_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if snapshot and snapshot["nav_per_unit"]:
        return float(snapshot["nav_per_unit"])
    return float(get_setting(conn, "inception_nav_per_unit", 1000))


def lp_units_balance(conn, lp_id):
    return float(
        conn.execute(
            "SELECT COALESCE(SUM(units),0) units FROM lp_units WHERE lp_id=?", (lp_id,)
        ).fetchone()["units"]
    )


def record_cash_flow(conn, ts, amount, lp_id, note):
    timestamp, flow_date = _timestamp(ts)
    amount = float(amount)
    if not conn.execute("SELECT 1 FROM lps WHERE id=?", (lp_id,)).fetchone():
        raise ValueError("LP not found")
    nav_per_unit = current_nav_per_unit(conn)
    if amount < 0 and abs(amount) > lp_units_balance(conn, lp_id) * nav_per_unit + 1e-9:
        raise ValueError("Withdrawal exceeds LP value")
    flow = conn.execute(
        "INSERT INTO cash_flows(ts,amount,lp_id,note) VALUES (?,?,?,?)",
        (timestamp, amount, lp_id, note),
    )
    unit_delta = amount / nav_per_unit if nav_per_unit else 0
    conn.execute(
        "INSERT INTO lp_units(lp_id,ts,units,nav_per_unit,cash_flow_id) VALUES (?,?,?,?,?)",
        (lp_id, timestamp, unit_delta, nav_per_unit, flow.lastrowid),
    )
    conn.commit()
    return {
        "cash_flow_id": flow.lastrowid,
        "units": unit_delta,
        "nav_per_unit": nav_per_unit,
        "date": flow_date,
    }


def ownership(conn):
    nav_per_unit = current_nav_per_unit(conn)
    total_units = float(
        conn.execute("SELECT COALESCE(SUM(units),0) units FROM lp_units").fetchone()[
            "units"
        ]
    )
    rows = []
    for lp in conn.execute("SELECT * FROM lps ORDER BY name").fetchall():
        units = lp_units_balance(conn, lp["id"])
        contributed, withdrawn = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END),0) "
            "FROM cash_flows WHERE lp_id=?",
            (lp["id"],),
        ).fetchone()
        value = units * nav_per_unit
        rows.append(
            {
                "id": lp["id"],
                "name": lp["name"],
                "is_gp": bool(lp["is_gp"]),
                "units": units,
                "percentage": units / total_units if total_units else 0,
                "value": value,
                "contributed": float(contributed),
                "withdrawn": float(withdrawn),
                "pnl": value + float(withdrawn) - float(contributed),
            }
        )
    return {
        "rows": rows,
        "total_units": total_units,
        "nav_per_unit": nav_per_unit,
        "total_value": total_units * nav_per_unit,
    }


def _event_timestamp(ts):
    return ts.isoformat() if isinstance(ts, datetime) else str(ts)


def crystallize_perf_fee(conn, ts):
    units = float(
        conn.execute("SELECT COALESCE(SUM(units),0) units FROM lp_units").fetchone()[
            "units"
        ]
    )
    gross_nav = float(compute_portfolio(conn)["nav"])
    liability = float(get_setting(conn, "fee_liability", 0) or 0)
    nav_per_unit = (gross_nav - liability) / units if units else 0
    hwm = float(get_setting(conn, "hwm_per_unit", 1000) or 0)
    perf_pct = float(get_setting(conn, "perf_fee_pct", 20) or 0)
    fee = max(0.0, nav_per_unit - hwm) * units * perf_pct / 100
    if fee > 0:
        set_setting(conn, "fee_liability", float(get_setting(conn, "fee_liability", 0) or 0) + fee)
        conn.execute(
            "INSERT INTO fee_events(ts,kind,amount,hwm_before,hwm_after,note) "
            "VALUES (?,?,?,?,?,?)",
            (
                _event_timestamp(ts),
                "perf",
                fee,
                hwm,
                nav_per_unit,
                "Performance fee crystallization",
            ),
        )
        set_setting(conn, "hwm_per_unit", nav_per_unit)
        conn.commit()
    return fee


def settle_fees(conn, ts):
    liability = float(get_setting(conn, "fee_liability", 0) or 0)
    if liability <= 0:
        return 0.0
    gp = conn.execute("SELECT id FROM lps WHERE is_gp=1 ORDER BY id LIMIT 1").fetchone()
    if not gp:
        raise ValueError("Set a GP first")
    nav_per_unit = current_nav_per_unit(conn)
    timestamp = _event_timestamp(ts)
    flow = conn.execute(
        "INSERT INTO cash_flows(ts,amount,lp_id,note) VALUES (?,?,?,?)",
        (timestamp, 0, gp["id"], "Fee settlement"),
    )
    units = liability / nav_per_unit if nav_per_unit else 0
    conn.execute(
        "INSERT INTO lp_units(lp_id,ts,units,nav_per_unit,cash_flow_id) VALUES (?,?,?,?,?)",
        (gp["id"], timestamp, units, nav_per_unit, flow.lastrowid),
    )
    conn.execute(
        "INSERT INTO fee_events(ts,kind,amount,note) VALUES (?,?,?,?)",
        (timestamp, "settle", liability, "Fees paid to GP"),
    )
    set_setting(conn, "fee_liability", 0)
    conn.commit()
    return units


def fee_scenario(
    starting_nav,
    starting_nav_per_unit=1000,
    hwm_per_unit=None,
    gross_return_pct=0,
    mgmt_pct=2,
    perf_pct=20,
):
    starting_nav = float(starting_nav)
    starting_nav_per_unit = float(starting_nav_per_unit)
    hwm_per_unit = (
        starting_nav_per_unit if hwm_per_unit is None else float(hwm_per_unit)
    )
    units = starting_nav / starting_nav_per_unit if starting_nav_per_unit else 0
    gross_pnl = starting_nav * float(gross_return_pct) / 100
    mgmt_fee = starting_nav * float(mgmt_pct) / 100
    gross_end_npu = starting_nav_per_unit * (1 + float(gross_return_pct) / 100)
    npu_after_mgmt = gross_end_npu - mgmt_fee / units if units else gross_end_npu
    perf_fee = (
        max(0.0, npu_after_mgmt - hwm_per_unit)
        * units
        * float(perf_pct)
        / 100
    )
    net_end_npu = npu_after_mgmt - perf_fee / units if units else npu_after_mgmt
    net_to_lps = net_end_npu * units
    return {
        "gross_pnl": gross_pnl,
        "mgmt_fee": mgmt_fee,
        "perf_fee": perf_fee,
        "gp_take": mgmt_fee + perf_fee,
        "net_to_lps": net_to_lps,
        "net_return_pct": (net_to_lps / starting_nav - 1) * 100 if starting_nav else 0,
        "new_hwm": max(hwm_per_unit, net_end_npu),
    }
