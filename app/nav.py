from __future__ import annotations

import asyncio
import csv
import io
import json
import math
from datetime import UTC, date, datetime
from itertools import pairwise

from .benchmark import beta, pair_returns
from .db import get_setting, set_setting
from .positions import build_positions, cash_from_trades
from .pricing import refresh_prices, yahoo_symbol_for

_BENCHMARK_UNSET = object()


def _price_age_seconds(ts, now):
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (now - parsed).total_seconds())
    except ValueError:
        return None


def compute_portfolio(conn, mark_overrides=None):
    mark_overrides = mark_overrides or {}
    now = datetime.now(UTC)
    try:
        failed_symbols = set(json.loads(get_setting(conn, "last_refresh_failures", "[]")))
    except (TypeError, json.JSONDecodeError):
        failed_symbols = set()
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
        quote_symbol = yahoo_symbol_for(instrument)
        failed = quote_symbol in failed_symbols
        if instrument["id"] in mark_overrides:
            mark = float(mark_overrides[instrument["id"]])
        elif instrument["pricing_source"] == "manual":
            mark = (
                float(instrument["manual_mark"])
                if instrument["manual_mark"] is not None
                else None
            )
        elif price_row and not failed:
            mark = float(price_row["price"])
        else:
            mark = None
        calculation_mark = mark if mark is not None else 0.0
        mult = float(instrument["multiplier"])
        market_value = pos.qty * calculation_mark * mult
        unrealized = (calculation_mark - pos.avg_price) * pos.qty * mult
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
                "price_age_seconds": _price_age_seconds(
                    price_row["ts"] if price_row else None, now
                ),
                "price_failed": bool(failed),
                "manual_mark_at": instrument["manual_mark_at"],
            }
        )
    cash = float(flows) + cash_from_trades(trades)
    net_exposure = gross_long - gross_short
    nav = cash + net_exposure
    fee_liability = float(get_setting(conn, "fee_liability", 0) or 0)
    leverage = float(get_setting(conn, "leverage", 1.0))
    return {
        "positions": sorted(rows, key=lambda row: abs(row["market_value"]), reverse=True),
        "cash": cash,
        "gross_long": gross_long,
        "gross_short": gross_short,
        "net_exposure": net_exposure,
        "nav": nav,
        "gross_nav": nav,
        "fee_liability": fee_liability,
        "net_nav": nav - fee_liability,
        "leverage_target": leverage,
        "target_gross": leverage * nav,
        "current_gross_leverage": (gross_long + gross_short) / nav if nav else 0,
    }


def exposure_by_class(portfolio):
    grouped = {}
    for position in portfolio["positions"]:
        asset_class = position["asset_class"]
        row = grouped.setdefault(
            asset_class,
            {
                "class": asset_class,
                "asset_class": asset_class,
                "long": 0.0,
                "short": 0.0,
                "net": 0.0,
                "gross": 0.0,
                "unrealized": 0.0,
            },
        )
        market_value = float(position["market_value"])
        if market_value >= 0:
            row["long"] += market_value
        else:
            row["short"] += -market_value
        row["net"] += market_value
        row["gross"] += abs(market_value)
        row["unrealized"] += float(position["unrealized"])
    nav = float(portfolio.get("net_nav", portfolio.get("nav", 0)) or 0)
    rows = sorted(grouped.values(), key=lambda row: row["gross"], reverse=True)
    for row in rows:
        row["pct_nav"] = row["gross"] / nav if nav else 0.0
        row["pct_of_nav"] = row["pct_nav"]
    total = {
        "class": "Total",
        "asset_class": "Total",
        "long": sum(row["long"] for row in rows),
        "short": sum(row["short"] for row in rows),
        "net": sum(row["net"] for row in rows),
        "gross": sum(row["gross"] for row in rows),
        "unrealized": sum(row["unrealized"] for row in rows),
    }
    total["pct_nav"] = total["gross"] / nav if nav else 0.0
    total["pct_of_nav"] = total["pct_nav"]
    return rows + [total]


def _snapshot_row(conn, snapshot_date):
    return conn.execute(
        "SELECT * FROM nav_snapshots WHERE date=?", (snapshot_date.isoformat(),)
    ).fetchone()


def take_snapshot(
    conn,
    snapshot_date: date,
    source: str = "manual",
    refresh: bool = False,
    fetch_benchmark: bool = True,
    benchmark_close=_BENCHMARK_UNSET,
) -> dict:
    existing = _snapshot_row(conn, snapshot_date)
    if refresh:
        asyncio.run(refresh_prices(conn))
    live_portfolio = compute_portfolio(conn)
    previous_marks = {}
    for row in conn.execute(
            "SELECT sm.instrument_id,sm.mark FROM snapshot_marks sm "
            "JOIN nav_snapshots ns ON ns.date=sm.date "
            "WHERE ns.date<? ORDER BY ns.date DESC",
            (snapshot_date.isoformat(),),
        ).fetchall():
        previous_marks.setdefault(row["instrument_id"], row["mark"])
    mark_overrides = {}
    snapshot_mark_rows = []
    for position in live_portfolio["positions"]:
        if position["pricing_source"] == "manual" and position["mark"] is not None:
            mark_overrides[position["instrument_id"]] = position["mark"]
            snapshot_mark_rows.append(
                (position["instrument_id"], position["mark"], "manual")
            )
        elif not position["price_failed"] and position["mark"] is not None:
            mark_overrides[position["instrument_id"]] = position["mark"]
            snapshot_mark_rows.append(
                (position["instrument_id"], position["mark"], "yahoo")
            )
        elif position["instrument_id"] in previous_marks:
            mark_overrides[position["instrument_id"]] = previous_marks[
                position["instrument_id"]
            ]
            snapshot_mark_rows.append(
                (
                    position["instrument_id"],
                    previous_marks[position["instrument_id"]],
                    "snapshot",
                )
            )
        else:
            mark_overrides[position["instrument_id"]] = position["avg_price"]
            snapshot_mark_rows.append(
                (position["instrument_id"], position["avg_price"], "fallback")
            )
    marked_instruments = {instrument_id for instrument_id, _, _ in snapshot_mark_rows}
    traded_today = conn.execute(
        "SELECT DISTINCT instrument_id FROM trades WHERE substr(ts,1,10)=?",
        (snapshot_date.isoformat(),),
    ).fetchall()
    failed_symbols = set(json.loads(get_setting(conn, "last_refresh_failures", "[]") or "[]"))
    for row in traded_today:
        instrument_id = row["instrument_id"]
        if instrument_id in marked_instruments:
            continue
        instrument = conn.execute(
            "SELECT * FROM instruments WHERE id=?", (instrument_id,)
        ).fetchone()
        if not instrument:
            continue
        mark = None
        mark_source = "fallback"
        if instrument["pricing_source"] == "manual" and instrument["manual_mark"] is not None:
            mark = float(instrument["manual_mark"])
            mark_source = "manual"
        else:
            price = conn.execute(
                "SELECT price FROM prices WHERE instrument_id=?", (instrument_id,)
            ).fetchone()
            if price and yahoo_symbol_for(instrument) not in failed_symbols:
                mark = float(price["price"])
                mark_source = "yahoo"
        if mark is None and instrument_id in previous_marks:
            mark = float(previous_marks[instrument_id])
            mark_source = "snapshot"
        if mark is None:
            trade = conn.execute(
                "SELECT price FROM trades WHERE instrument_id=? ORDER BY ts DESC,id DESC LIMIT 1",
                (instrument_id,),
            ).fetchone()
            mark = float(trade["price"]) if trade else 0.0
        snapshot_mark_rows.append((instrument_id, mark, mark_source))
    portfolio = compute_portfolio(conn, mark_overrides)
    gross_nav = float(portfolio["nav"])
    liability = float(get_setting(conn, "fee_liability", 0) or 0)
    first_snapshot = existing is None and not conn.execute(
        "SELECT 1 FROM nav_snapshots LIMIT 1"
    ).fetchone()
    units = float(
        conn.execute("SELECT COALESCE(SUM(units),0) units FROM lp_units").fetchone()[
            "units"
        ]
    )
    if units == 0 and gross_nav > 0:
        inception = float(get_setting(conn, "inception_nav_per_unit", 1000))
        units = (gross_nav - liability) / inception if gross_nav - liability > 0 else 0
        lp_id = conn.execute("SELECT id FROM lps WHERE name='Principal'").fetchone()["id"]
        conn.execute(
            "INSERT INTO lp_units(lp_id,ts,units,nav_per_unit) VALUES (?,?,?,?)",
            (lp_id, datetime.now(UTC).isoformat(), units, inception),
        )
    mgmt_fee_accrued = 0.0
    if existing is None and not first_snapshot:
        from .fees import accrue_management_fees

        fee = accrue_management_fees(
            conn,
            snapshot_date,
            gross_nav,
            (gross_nav - liability) / units if units else 0,
        )
        liability += fee
        set_setting(conn, "fee_liability", liability)
        mgmt_fee_accrued = fee
    if existing is None and _is_last_trading_day_of_year(snapshot_date):
        from .fees import crystallize_perf_fee

        crystallize_perf_fee(conn, datetime.now(UTC))
        liability = float(get_setting(conn, "fee_liability", 0) or 0)
    net_nav = gross_nav - liability
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
    if existing is not None:
        mgmt_fee_accrued = float(existing["mgmt_fee_accrued"])
    values = {
        "date": snapshot_date.isoformat(),
        "ts": datetime.now(UTC).isoformat(),
        "nav": net_nav,
        "cash": portfolio["cash"],
        "gross_long": portfolio["gross_long"],
        "gross_short": portfolio["gross_short"],
        "net_exposure": portfolio["net_exposure"],
        "flows_today": float(flows_today),
        "units_outstanding": units,
        "nav_per_unit": nav_per_unit,
        "daily_return": daily_return,
        "levered_return": levered_return,
        "mgmt_fee_accrued": mgmt_fee_accrued,
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
    conn.execute("DELETE FROM snapshot_marks WHERE date=?", (values["date"],))
    conn.executemany(
        "INSERT INTO snapshot_marks(date,instrument_id,mark,source) VALUES (?,?,?,?)",
        [(values["date"], instrument_id, mark, mark_source) for instrument_id, mark, mark_source in snapshot_mark_rows],
    )
    benchmark_symbol = str(get_setting(conn, "benchmark_symbol", "") or "").strip().upper()
    if benchmark_symbol and fetch_benchmark:
        if benchmark_close is _BENCHMARK_UNSET:
            from .pricing import fetch_benchmark_close

            try:
                benchmark_close = asyncio.run(
                    fetch_benchmark_close(benchmark_symbol, snapshot_date)
                )
            except (RuntimeError, OSError):
                benchmark_close = None
        conn.execute(
            "INSERT INTO benchmark_closes(symbol,date,close) VALUES (?,?,?) "
            "ON CONFLICT(symbol,date) DO UPDATE SET close=excluded.close",
            (benchmark_symbol, values["date"], benchmark_close),
        )
    conn.commit()
    return dict(conn.execute("SELECT * FROM nav_snapshots WHERE date=?", (values["date"],)).fetchone())


def _is_last_trading_day_of_year(snapshot_date):
    import holidays

    holidays_nyse = holidays.financial_holidays("NYSE")
    if snapshot_date.weekday() >= 5 or snapshot_date in holidays_nyse:
        return False
    following = snapshot_date.fromordinal(snapshot_date.toordinal() + 1)
    while following.year == snapshot_date.year:
        if following.weekday() < 5 and following not in holidays_nyse:
            return False
        following = following.fromordinal(following.toordinal() + 1)
    return True


def history_series(conn):
    rows = [dict(row) for row in conn.execute("SELECT * FROM nav_snapshots ORDER BY date").fetchall()]
    benchmark_symbol = str(get_setting(conn, "benchmark_symbol", "") or "").strip().upper()
    benchmark_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT date,close FROM benchmark_closes WHERE symbol=? ORDER BY date",
            (benchmark_symbol,),
        ).fetchall()
    ] if benchmark_symbol else []
    pairs = pair_returns(rows, benchmark_rows)
    benchmark_closes = sorted(
        (row["date"], float(row["close"]))
        for row in benchmark_rows
        if row["close"] is not None
    )
    benchmark_returns = {
        current[0]: current[1] / previous[1] - 1
        for previous, current in pairwise(benchmark_closes)
        if previous[1]
    }
    paired = {
        row["date"]: benchmark_returns[row["date"]]
        for row in rows
        if row["date"] in benchmark_returns and row["daily_return"] is not None
    }
    cumulative = 1.0
    cumulative_levered = 1.0
    benchmark_cumulative = 1.0
    benchmark_values = []
    returns = []
    return_days = []
    for row in rows:
        if row["daily_return"] is not None:
            cumulative *= 1 + row["daily_return"]
            returns.append(row["daily_return"])
            return_days.append((row["date"], row["daily_return"]))
        if row["levered_return"] is not None:
            cumulative_levered *= 1 + row["levered_return"]
        row["cumulative_return"] = cumulative - 1 if returns else None
        row["cumulative_levered_return"] = cumulative_levered - 1 if row["levered_return"] is not None else None
        row["benchmark_return"] = paired.get(row["date"])
        row["excess_return"] = (
            row["daily_return"] - row["benchmark_return"]
            if row["daily_return"] is not None and row["benchmark_return"] is not None
            else None
        )
        if row["benchmark_return"] is not None:
            benchmark_cumulative *= 1 + row["benchmark_return"]
        benchmark_values.append(benchmark_cumulative)
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
    has_benchmark = bool(pairs)
    summary = {
        "inception_return": cumulative - 1 if returns else None,
        "annualized_return": (cumulative ** (252 / count) - 1) if count else None,
        "annualized_vol": vol,
        "sharpe": (mean / math.sqrt(variance) * math.sqrt(252)) if variance else None,
        "max_drawdown": min(peaks) if peaks else None,
        "best_day": max(returns) if returns else None,
        "best_day_date": max(return_days, key=lambda item: item[1])[0] if returns else None,
        "worst_day": min(returns) if returns else None,
        "worst_day_date": min(return_days, key=lambda item: item[1])[0] if returns else None,
        "benchmark_return": benchmark_cumulative - 1 if has_benchmark else None,
        "excess_return": (
            cumulative - benchmark_cumulative if has_benchmark and returns else None
        ),
        "beta": beta(pairs) if len(pairs) >= 10 else None,
    }
    chart_points = []
    benchmark_points = []
    if nav_units:
        normalized_benchmark = (
            [nav_units[0] * value for value in benchmark_values]
            if has_benchmark
            else []
        )
        chart_values = nav_units + normalized_benchmark
        low, high = min(chart_values), max(chart_values)
        span = high - low or 1
        chart_points = [
            f"{(index / max(len(nav_units) - 1, 1)) * 1000:.1f},{150 - ((value - low) / span) * 140:.1f}"
            for index, value in enumerate(nav_units)
        ]
        benchmark_points = [
            f"{(index / max(len(normalized_benchmark) - 1, 1)) * 1000:.1f},{150 - ((value - low) / span) * 140:.1f}"
            for index, value in enumerate(normalized_benchmark)
        ]
        if len(chart_points) == 1:
            chart_points.append(chart_points[0].replace("0.0,", "1000.0,", 1))
    else:
        low = high = 0
    chart_inception_y = (
        150 - ((1000 - low) / (high - low or 1)) * 140 if nav_units else 150
    )
    return {
        "snapshots": rows,
        "summary": summary,
        "chart": nav_units,
        "chart_points": " ".join(chart_points),
        "benchmark_points": " ".join(benchmark_points),
        "chart_min": low,
        "chart_max": high,
        "chart_inception_y": chart_inception_y,
        "benchmark_values": benchmark_values,
        "benchmark_symbol": benchmark_symbol,
    }


def history_csv(conn) -> str:
    series = history_series(conn)
    output = io.StringIO()
    fields = [
        "date", "nav", "nav_per_unit", "daily_return", "levered_return",
        "benchmark_return", "cumulative_return", "flows_today", "gross_long",
        "gross_short", "cash", "source",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in reversed(series["snapshots"]):
        writer.writerow({field: row.get(field) for field in fields})
    return output.getvalue()
