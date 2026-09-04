from .db import get_setting


def fx_rate_for(conn, currency: str) -> float | None:
    currency = str(currency or "").strip().upper()
    base = str(get_setting(conn, "base_currency", "USD") or "USD").strip().upper()
    if currency == base:
        return 1.0
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE currency=?", (currency,)
    ).fetchone()
    return float(row["rate"]) if row else None


def trade_fx_rate(conn, instrument_id: int, value=None) -> float:
    if value not in (None, ""):
        return float(value)
    row = conn.execute(
        "SELECT currency FROM instruments WHERE id=?", (instrument_id,)
    ).fetchone()
    rate = fx_rate_for(conn, row["currency"]) if row else None
    return rate if rate is not None else 1.0
