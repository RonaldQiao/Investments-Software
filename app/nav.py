from __future__ import annotations

import asyncio
import csv
import io
import math
from datetime import date, datetime, timezone

from .db import get_setting, set_setting
from .pricing import refresh_prices
from .positions import build_positions, cash_from_trades


def compute_portfolio(conn):
    instruments = conn.execute("SELECT * FROM instruments ORDER BY symbol").fetchall()
    trades = conn.execute(
        "SELECT t.*, i.multiplier FROM trades t JOIN instruments i ON i.id=t.instrument_id "
        "ORDER BY t.ts,t.id"
    ).fetchall()
    flows = conn.execute("SELECT COALESCE(SUM(amount),0) amount FROM cash_flows").fetchone()[
        "amount"
    ]
    positions = build_positions(trades)
    rows = []
    gross_long = gross_short = 0.0
    price_rows = {
        row["instrument_id"]: row
        for row in conn.execute("SELECT * FROM prices").fetchall()
    }
    for instrument in instruments:
        pos = positions.get(instrument["id"])
        if not pos or pos.qty == 0:
            continue
        price_row = price_rows.get(instrument["id"])
        mark = (
            float(instrument["manual_mark"])
            if instrument["pricing_source"] == "manual" and instrument["manual_mark"] is not None
            else (float(price_row["price"]) if price_row else instrument["manual_mark"])
        )
        if mark is None:
            mark = 0.0
        mult = float(instrument["multiplier"])
        market_value = pos.qty * mark * mult
        unrealized = (mark - pos.avg_price) * pos.qty * mult
        if market_value >= 0:
            gross_long += market_value
        else:
            gross_short += -market_value
        rows.append(
            {
                "instrument_id": instrument["id"],
                "symbol": instrument["symbol"],
                "name": instrument["name"],
                "asset_class": instrument["asset_class"],
                "currency": instrument["currency"],
                "multiplier": mult,
                "pricing_source": instrument["pricing_source"],
                "notes": instrument["notes"] or "",
                "qty": pos.qty,
                "side": "LONG" if pos.qty > 0 else "SHORT",
                "avg_price": pos.avg_price,
                "mark": mark,
                "market_value": market_value,
                "unrealized": unrealized,
                "realized": pos.realized_pnl,
                "price_ts": price_row["ts"] if price_row else None,
            }
        )
    cash = float(flows) + cash_from_trades(trades)
    net_exposure = gross_long - gross_short
    nav = cash + net_exposure
    leverage = float(get_setting(conn, "leverage", 1.0))
    return {
        "positions": sorted(rows, key=lambda row: abs(row["market_value"]), reverse=True),
        "cash": cash,
        "gross_long": gross_long,
        "gross_short": gross_short,
        "net_exposure": net_exposure,
        "nav": nav,
        "leverage_target": leverage,
        "target_gross": leverage * nav,
        "current_gross_leverage": (gross_long + gross_short) / nav if nav else 0,
    }


def _snapshot_row(conn, snapshot_date):
    return conn.execute(
        "SELECT * FROM nav_snapshots WHERE date=?", (snapshot_date.isoformat(),)
    ).fetchone()


def take_snapshot(
    conn, snapshot_date: date, source: str = "manual", refresh: bool = False
) -> dict:
    existing = _snapshot_row(conn, snapshot_date)
    if refresh:
        asyncio.run(refresh_prices(conn))
    portfolio = compute_portfolio(conn)
    gross_nav = float(portfolio["nav"])
    liability = float(get_setting(conn, "fee_liability", 0) or 0)
    first_snapshot = (
        existing is None
        and conn.execute("SELECT 1 FROM nav_snapshots LIMIT 1").fetchone() is None
    )
    if existing is None and not first_snapshot:
        fee = gross_nav * float(get_setting(conn, "mgmt_fee_bps", 200)) / 10000 / 252
        liability += fee
        set_setting(conn, "fee_liability", liability)
        conn.execute(
            "INSERT INTO fee_events(ts,kind,amount,note) VALUES (?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                "mgmt",
                fee,
                f"Management fee for {snapshot_date.isoformat()}",
            ),
        )
    net_nav = gross_nav - liability
    units = float(
        conn.execute("SELECT COALESCE(SUM(units),0) units FROM lp_units").fetchone()[
            "units"
        ]
    )
    if units == 0 and gross_nav > 0:
        inception = float(get_setting(conn, "inception_nav_per_unit", 1000))
        units = net_nav / inception if net_nav else 0
        lp_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
        conn.execute(
            "INSERT INTO lp_units(lp_id,ts,units,nav_per_unit) VALUES (?,?,?,?)",
            (lp_id, datetime.now(timezone.utc).isoformat(), units, inception),
        )
    nav_per_unit = net_nav / units if units else 0
    previous = conn.execute(
        "SELECT nav_per_unit FROM nav_snapshots WHERE date<? ORDER BY date DESC LIMIT 1",
        (snapshot_date.isoformat(),),
    ).fetchone()
    daily_return = (
        nav_per_unit / previous["nav_per_unit"] - 1
        if previous and previous["nav_per_unit"]
        else None
    )
    leverage = float(get_setting(conn, "leverage", 1.0))
    borrow_rate = float(get_setting(conn, "borrow_rate", 0.05))
    levered_return = (
        leverage * daily_return - (leverage - 1) * borrow_rate / 252
        if daily_return is not None
        else None
    )
    flows_today = conn.execute(
        "SELECT COALESCE(SUM(amount),0) amount FROM cash_flows WHERE substr(ts,1,10)=?",
        (snapshot_date.isoformat(),),
    ).fetchone()["amount"]
    values = {
        "date": snapshot_date.isoformat(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "nav": net_nav,
        "cash": portfolio["cash"] - liability,
        "gross_long": portfolio["gross_long"],
        "gross_short": portfolio["gross_short"],
        "net_exposure": portfolio["net_exposure"],
        "flows_today": float(flows_today),
        "units_outstanding": units,
        "nav_per_unit": nav_per_unit,
        "daily_return": daily_return,
        "levered_return": levered_return,
            "mgmt_fee_accrued": (
                float(
                    conn.execute(
                        "SELECT amount FROM fee_events WHERE kind='mgmt' AND note=? "
                        "ORDER BY id DESC LIMIT 1",
                        (f"Management fee for {snapshot_date.isoformat()}",),
                    ).fetchone()["amount"]
                )
                if existing is None and not first_snapshot
                else (0.0 if first_snapshot else float(existing["mgmt_fee_accrued"]))
            ),
        "source": source,
    }
    conn.execute(
        "INSERT INTO nav_snapshots(date,ts,nav,cash,gross_long,gross_short,net_exposure,"
        "flows_today,units_outstanding,nav_per_unit,daily_return,levered_return,"
        "mgmt_fee_accrued,source) VALUES (:date,:ts,:nav,:cash,:gross_long,:gross_short,"
        ":net_exposure,:flows_today,:units_outstanding,:nav_per_unit,:daily_return,"
        ":levered_return,:mgmt_fee_accrued,:source) ON CONFLICT(date) DO UPDATE SET "
        "ts=excluded.ts,nav=excluded.nav,cash=excluded.cash,gross_long=excluded.gross_long,"
        "gross_short=excluded.gross_short,net_exposure=excluded.net_exposure,"
        "flows_today=excluded.flows_today,units_outstanding=excluded.units_outstanding,"
        "nav_per_unit=excluded.nav_per_unit,daily_return=excluded.daily_return,"
        "levered_return=excluded.levered_return,mgmt_fee_accrued=excluded.mgmt_fee_accrued,"
        "source=excluded.source",
        values,
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM nav_snapshots WHERE date=?", (values["date"],)).fetchone())


def history_series(conn):
    rows = [dict(row) for row in conn.execute("SELECT * FROM nav_snapshots ORDER BY date").fetchall()]
    cumulative = 1.0
    cumulative_levered = 1.0
    returns = []
    for row in rows:
        if row["daily_return"] is not None:
            cumulative *= 1 + row["daily_return"]
            returns.append(row["daily_return"])
        if row["levered_return"] is not None:
            cumulative_levered *= 1 + row["levered_return"]
        row["cumulative_return"] = cumulative - 1 if returns else None
        row["cumulative_levered_return"] = cumulative_levered - 1 if row["levered_return"] is not None else None
    nav_units = [row["nav_per_unit"] for row in rows if row["nav_per_unit"]]
    peaks = []
    peak = None
    for value in nav_units:
        peak = value if peak is None else max(peak, value)
        peaks.append(value / peak - 1)
    count = len(returns)
    mean = sum(returns) / count if count else 0
    variance = sum((item - mean) ** 2 for item in returns) / (count - 1) if count > 1 else 0
    vol = math.sqrt(variance) * math.sqrt(252)
    summary = {
        "inception_return": cumulative - 1 if returns else None,
        "annualized_return": (cumulative ** (252 / count) - 1) if count else None,
        "annualized_vol": vol,
        "sharpe": (mean / math.sqrt(variance) * math.sqrt(252)) if variance else None,
        "max_drawdown": min(peaks) if peaks else None,
        "best_day": max(returns) if returns else None,
        "worst_day": min(returns) if returns else None,
    }
    chart_points = []
    if nav_units:
        low, high = min(nav_units), max(nav_units)
        span = high - low or 1
        chart_points = [
            f"{(index / max(len(nav_units) - 1, 1)) * 1000:.1f},{150 - ((value - low) / span) * 140:.1f}"
            for index, value in enumerate(nav_units)
        ]
    else:
        low = high = 0
    return {
        "snapshots": rows,
        "summary": summary,
        "chart": nav_units,
        "chart_points": " ".join(chart_points),
        "chart_min": low,
        "chart_max": high,
    }


def history_csv(conn) -> str:
    series = history_series(conn)
    output = io.StringIO()
    fields = [
        "date", "nav", "nav_per_unit", "daily_return", "levered_return",
        "cumulative_return", "flows_today", "gross_long", "gross_short", "cash", "source",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in reversed(series["snapshots"]):
        writer.writerow({field: row.get(field) for field in fields})
    return output.getvalue()
