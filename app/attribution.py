from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def _as_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _trade_date(value):
    text = str(value)
    if "T" not in text:
        return date.fromisoformat(text[:10])
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=NY)
    return parsed.astimezone(NY).date()


def _realized_by_day(conn, start_date, end_date):
    trades = conn.execute(
        "SELECT t.*,i.multiplier,i.asset_class,i.symbol FROM trades t "
        "JOIN instruments i ON i.id=t.instrument_id ORDER BY t.ts,t.id"
    ).fetchall()
    states = {}
    realized = defaultdict(float)
    for trade in trades:
        trade_day = _trade_date(trade["ts"])
        qty = float(trade["quantity"])
        delta = qty if trade["side"] == "BUY" else -qty
        price = float(trade["price"])
        multiplier = float(trade["multiplier"])
        state = states.setdefault(trade["instrument_id"], [0.0, 0.0])
        if state[0] == 0:
            state[:] = [delta, price]
        elif state[0] * delta > 0:
            total = abs(state[0]) + abs(delta)
            state[1] = (abs(state[0]) * state[1] + abs(delta) * price) / total
            state[0] += delta
        else:
            close_qty = min(abs(state[0]), abs(delta))
            pnl = (price - state[1]) * close_qty * (1 if state[0] > 0 else -1) * multiplier
            if start_date <= trade_day <= end_date:
                realized[(trade["instrument_id"], trade_day)] += pnl
            state[0] += delta
            if state[0] == 0:
                state[1] = 0.0
            elif abs(delta) > abs(state[0] - delta):
                state[1] = price
    return realized


def attribution(conn, start_date, end_date):
    start_date = _as_date(start_date)
    end_date = _as_date(end_date)
    snapshots = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM nav_snapshots WHERE date>=? AND date<=? ORDER BY date",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    ]
    if len(snapshots) < 2:
        return {"rows": [], "classes": [], "total": {"pnl": 0.0}, "snapshots": snapshots}
    instruments = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM instruments").fetchall()
    }
    marks_by_date = {}
    for snapshot in snapshots:
        marks_by_date[snapshot["date"]] = {
            row["instrument_id"]: float(row["mark"])
            for row in conn.execute(
                "SELECT instrument_id,mark FROM snapshot_marks WHERE date=?",
                (snapshot["date"],),
            ).fetchall()
        }
    realized = _realized_by_day(conn, start_date, end_date)
    trades = conn.execute(
        "SELECT t.instrument_id,t.ts,t.side,t.quantity "
        "FROM trades t ORDER BY t.ts,t.id"
    ).fetchall()
    rows_by_id = {}
    daily_by_id = defaultdict(list)
    for previous, current in zip(snapshots, snapshots[1:]):
        previous_marks = marks_by_date[previous["date"]]
        current_marks = marks_by_date[current["date"]]
        day = date.fromisoformat(current["date"])
        quantities = defaultdict(float)
        for trade in trades:
            if _trade_date(trade["ts"]) <= day:
                quantities[trade["instrument_id"]] += (
                    float(trade["quantity"])
                    if trade["side"] == "BUY"
                    else -float(trade["quantity"])
                )
        for instrument_id in set(previous_marks) | set(current_marks):
            if instrument_id not in instruments or instrument_id not in previous_marks or instrument_id not in current_marks:
                continue
            instrument = instruments[instrument_id]
            mark_pnl = quantities[instrument_id] * (
                current_marks[instrument_id] - previous_marks[instrument_id]
            ) * float(instrument["multiplier"])
            day_pnl = mark_pnl + realized[(instrument_id, day)]
            row = rows_by_id.setdefault(
                instrument_id,
                {
                    "instrument_id": instrument_id,
                    "symbol": instrument["symbol"],
                    "name": instrument["name"],
                    "asset_class": instrument["asset_class"],
                    "days_held": 0,
                    "pnl": 0.0,
                    "best_day": None,
                    "worst_day": None,
                },
            )
            row["days_held"] += 1 if quantities[instrument_id] else 0
            row["pnl"] += day_pnl
            daily_by_id[instrument_id].append((day, day_pnl))
    total_pnl = sum(row["pnl"] for row in rows_by_id.values())
    for instrument_id, row in rows_by_id.items():
        daily = daily_by_id[instrument_id]
        best = max(daily, key=lambda item: item[1], default=None)
        worst = min(daily, key=lambda item: item[1], default=None)
        row["best_day"] = {"date": best[0].isoformat(), "pnl": best[1]} if best else None
        row["worst_day"] = {"date": worst[0].isoformat(), "pnl": worst[1]} if worst else None
        row["pct_period"] = row["pnl"] / total_pnl if total_pnl else 0.0
    rows = sorted(rows_by_id.values(), key=lambda row: abs(row["pnl"]), reverse=True)
    classes = {}
    for row in rows:
        group = classes.setdefault(
            row["asset_class"],
            {"asset_class": row["asset_class"], "pnl": 0.0, "pct_period": 0.0},
        )
        group["pnl"] += row["pnl"]
    for group in classes.values():
        group["pct_period"] = group["pnl"] / total_pnl if total_pnl else 0.0
    return {
        "rows": rows,
        "classes": sorted(classes.values(), key=lambda row: abs(row["pnl"]), reverse=True),
        "total": {"pnl": total_pnl, "pct_period": 1.0 if total_pnl else 0.0},
        "snapshots": snapshots,
    }
