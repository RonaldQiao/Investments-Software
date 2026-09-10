from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from itertools import pairwise
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
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=NY)
    return parsed.astimezone(NY).date()


def attribution(conn, start_date, end_date):
    """Attribute audited snapshot-to-snapshot P&L without changing the ledger."""
    start_date = _as_date(start_date)
    end_date = _as_date(end_date)
    snapshots = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM nav_snapshots WHERE date>=? AND date<=? ORDER BY date",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    ]
    empty = {
        "rows": [],
        "classes": [],
        "total": {"pnl": 0.0, "pct_period": 0.0},
        "snapshots": snapshots,
    }
    if len(snapshots) < 2:
        return empty

    instruments = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM instruments").fetchall()
    }
    marks_by_date = {
        snapshot["date"]: {
            row["instrument_id"]: (float(row["mark"]), float(row["fx_rate"]))
            for row in conn.execute(
                "SELECT instrument_id,mark,fx_rate FROM snapshot_marks WHERE date=?",
                (snapshot["date"],),
            ).fetchall()
        }
        for snapshot in snapshots
    }
    trades = []
    for row in conn.execute(
        "SELECT t.id,t.instrument_id,t.ts,t.side,t.quantity,t.price,t.fees,t.fx_rate,"
        "i.multiplier FROM trades t JOIN instruments i ON i.id=t.instrument_id "
        "ORDER BY t.ts,t.id"
    ).fetchall():
        trade = dict(row)
        trade["trade_date"] = _trade_date(trade["ts"])
        trade["signed_qty"] = (
            float(trade["quantity"])
            if trade["side"] == "BUY"
            else -float(trade["quantity"])
        )
        trades.append(trade)

    quantities = defaultdict(float)
    trade_pointer = 0
    rows_by_id = {}
    daily_by_id = defaultdict(list)
    for previous, current in pairwise(snapshots):
        previous_day = date.fromisoformat(previous["date"])
        current_day = date.fromisoformat(current["date"])
        while (
            trade_pointer < len(trades)
            and trades[trade_pointer]["trade_date"] <= previous_day
        ):
            trade = trades[trade_pointer]
            quantities[trade["instrument_id"]] += trade["signed_qty"]
            trade_pointer += 1

        trades_today = []
        while (
            trade_pointer < len(trades)
            and trades[trade_pointer]["trade_date"] == current_day
        ):
            trades_today.append(trades[trade_pointer])
            trade_pointer += 1

        previous_marks = marks_by_date[previous["date"]]
        current_marks = marks_by_date[current["date"]]
        instrument_ids = set(previous_marks) | set(current_marks)
        instrument_ids.update(trade["instrument_id"] for trade in trades_today)
        for instrument_id in instrument_ids:
            instrument = instruments.get(instrument_id)
            if not instrument or instrument_id not in current_marks:
                continue
            mark, mark_fx = current_marks[instrument_id]
            multiplier = float(instrument["multiplier"])
            previous_mark = previous_marks.get(instrument_id)
            day_pnl = (
                quantities[instrument_id]
                * (mark * mark_fx - previous_mark[0] * previous_mark[1])
                * multiplier
                if previous_mark is not None
                else 0.0
            )
            for trade in trades_today:
                if trade["instrument_id"] == instrument_id:
                    day_pnl += (
                        trade["signed_qty"]
                        * (
                            mark * mark_fx
                            - float(trade["price"]) * float(trade["fx_rate"] or 1)
                        )
                        * multiplier
                        - float(trade["fees"] or 0) * float(trade["fx_rate"] or 1)
                    )
            if (
                instrument_id not in previous_marks
                and not any(
                    trade["instrument_id"] == instrument_id
                    for trade in trades_today
                )
            ):
                continue
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
            daily_by_id[instrument_id].append((current_day, day_pnl))

        for trade in trades_today:
            quantities[trade["instrument_id"]] += trade["signed_qty"]

    adjustment_labels = {
        "dividend": "Dividends",
        "interest": "Interest",
        "borrow": "Borrow",
        "fx": "FX",
        "fee": "Fees",
        "other": "Other",
    }
    adjustments = defaultdict(float)
    for adjustment in conn.execute(
        "SELECT ts,amount,category FROM cash_adjustments"
    ).fetchall():
        adjustment_date = _trade_date(adjustment["ts"])
        if start_date < adjustment_date <= end_date:
            category = str(adjustment["category"] or "other").lower()
            adjustments[category] += float(adjustment["amount"])
    for category, amount in adjustments.items():
        rows_by_id[("cash_adjustment", category)] = {
            "instrument_id": None,
            "symbol": adjustment_labels.get(category, category.title()),
            "name": adjustment_labels.get(category, category.title()),
            "asset_class": "cash",
            "days_held": 0,
            "pnl": amount,
            "best_day": None,
            "worst_day": None,
        }

    total_pnl = sum(row["pnl"] for row in rows_by_id.values())
    for instrument_id, row in rows_by_id.items():
        daily = daily_by_id[instrument_id]
        best = max(daily, key=lambda item: item[1], default=None)
        worst = min(daily, key=lambda item: item[1], default=None)
        row["best_day"] = (
            {"date": best[0].isoformat(), "pnl": best[1]} if best else None
        )
        row["worst_day"] = (
            {"date": worst[0].isoformat(), "pnl": worst[1]} if worst else None
        )
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
        "classes": sorted(
            classes.values(), key=lambda row: abs(row["pnl"]), reverse=True
        ),
        "total": {
            "pnl": total_pnl,
            "pct_period": 1.0 if total_pnl else 0.0,
        },
        "snapshots": snapshots,
    }
