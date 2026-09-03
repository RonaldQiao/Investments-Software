from __future__ import annotations

from .db import get_setting
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
    for instrument in instruments:
        pos = positions.get(instrument["id"])
        if not pos or pos.qty == 0:
            continue
        price_row = conn.execute(
            "SELECT * FROM prices WHERE instrument_id=?", (instrument["id"],)
        ).fetchone()
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
