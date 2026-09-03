from app.positions import build_positions, cash_from_trades


def t(instrument_id, side, quantity, price, multiplier=1, fees=0, ts="2024-01-01"):
    return {
        "instrument_id": instrument_id,
        "side": side,
        "quantity": quantity,
        "price": price,
        "multiplier": multiplier,
        "fees": fees,
        "ts": ts,
    }


def test_long_build_up_weighted_average():
    pos = build_positions([t(1, "BUY", 10, 100), t(1, "BUY", 10, 120, ts="2024-01-02")])[1]
    assert pos.qty == 20
    assert pos.avg_price == 110
    assert pos.realized_pnl == 0


def test_partial_sell_realizes_against_average():
    pos = build_positions([t(1, "BUY", 10, 100), t(1, "BUY", 10, 120, ts="2024-01-02"), t(1, "SELL", 5, 130, ts="2024-01-03")])[1]
    assert pos.qty == 15
    assert pos.avg_price == 110
    assert pos.realized_pnl == 100


def test_short_then_cover():
    pos = build_positions([t(1, "SELL", 5, 100), t(1, "BUY", 2, 80, ts="2024-01-02")])[1]
    assert pos.qty == -3
    assert pos.avg_price == 100
    assert pos.realized_pnl == 40


def test_cross_zero_long_to_short_reopens_at_trade_price():
    pos = build_positions([t(1, "BUY", 5, 100), t(1, "SELL", 8, 120, ts="2024-01-02")])[1]
    assert pos.qty == -3
    assert pos.avg_price == 120
    assert pos.realized_pnl == 100


def test_multiplier_applies_to_cash():
    trades = [t(1, "BUY", 2, 5000, multiplier=50, fees=3), t(1, "SELL", 1, 5100, multiplier=50)]
    assert cash_from_trades(trades) == -245003
