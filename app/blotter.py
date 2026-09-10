from __future__ import annotations

import re

_LINE = re.compile(
    r"^(?P<side>BUY|SELL|SHORT|COVER)\s+"
    r"(?P<quantity>(?:\d+(?:\.\d*)?|\.\d+))\s+"
    r"(?P<symbol>\S+)\s+@\s*"
    r"(?P<price>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?:\s+fee\s+(?P<fees>(?:\d+(?:\.\d*)?|\.\d+)))?"
    r"(?:\s+fx\s+(?P<fx_rate>[+-]?(?:\d+(?:\.\d*)?|\.\d+)))?$",
    re.IGNORECASE,
)


def parse_blotter(text: str) -> list[dict]:
    trades = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = _LINE.fullmatch(line)
        if not match:
            raise ValueError(f"Malformed blotter line {line_number}: {raw_line}")
        values = match.groupdict()
        side = values["side"].upper()
        trade = {
            "side": "BUY" if side == "COVER" else "SELL" if side == "SHORT" else side,
            "quantity": float(values["quantity"]),
            "symbol": values["symbol"].upper(),
            "price": float(values["price"]),
            "fees": float(values["fees"] or 0),
            "line": line_number,
        }
        if values["fx_rate"]:
            trade["fx_rate"] = float(values["fx_rate"])
        trades.append(trade)
    return trades
