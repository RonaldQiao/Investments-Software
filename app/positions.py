from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass
class Position:
    instrument_id: int
    qty: float
    avg_price: float
    realized_pnl: float
    avg_fx: float = 1.0


def _value(trade: Mapping, key: str, default=0):
    if hasattr(trade, "keys") and key in trade.keys():  # noqa: SIM118
        return trade[key]
    if hasattr(trade, "get"):
        return trade.get(key, default)
    return default


def build_positions(trades: list[Mapping]) -> dict[int, Position]:
    positions: dict[int, Position] = {}
    ordered = sorted(
        trades,
        key=lambda t: (str(_value(t, "ts", "")), int(_value(t, "id", 0) or 0)),
    )
    for trade in ordered:
        instrument_id = int(_value(trade, "instrument_id"))
        qty = float(_value(trade, "quantity"))
        delta = qty if _value(trade, "side") == "BUY" else -qty
        price = float(_value(trade, "price"))
        trade_fx = float(_value(trade, "fx_rate", 1) or 1)
        multiplier = float(_value(trade, "multiplier", 1) or 1)
        position = positions.setdefault(
            instrument_id, Position(instrument_id, 0.0, 0.0, 0.0)
        )
        if position.qty == 0:
            position.qty = delta
            position.avg_price = price
            position.avg_fx = trade_fx
            continue
        if position.qty * delta > 0:
            total = abs(position.qty) + abs(delta)
            position.avg_price = (
                abs(position.qty) * position.avg_price + abs(delta) * price
            ) / total
            position.avg_fx = (
                abs(position.qty) * position.avg_fx + abs(delta) * trade_fx
            ) / total
            position.qty += delta
            continue
        old_qty = position.qty
        close_qty = min(abs(position.qty), abs(delta))
        position.realized_pnl += (
            (price * trade_fx - position.avg_price * position.avg_fx)
            * close_qty
            * (1 if position.qty > 0 else -1)
            * multiplier
        )
        position.qty += delta
        if position.qty == 0:
            position.avg_price = 0.0
            position.avg_fx = 1.0
        elif abs(delta) > abs(old_qty):
            position.avg_price = price
            position.avg_fx = trade_fx
    return positions


def cash_from_trades(trades: Iterable[Mapping]) -> float:
    cash = 0.0
    for trade in trades:
        quantity = float(_value(trade, "quantity"))
        price = float(_value(trade, "price"))
        multiplier = float(_value(trade, "multiplier", 1) or 1)
        fees = float(_value(trade, "fees", 0) or 0)
        fx_rate = float(_value(trade, "fx_rate", 1) or 1)
        amount = quantity * price * multiplier * fx_rate
        cash += amount if _value(trade, "side") == "SELL" else -amount
        cash -= fees * fx_rate
    return cash
