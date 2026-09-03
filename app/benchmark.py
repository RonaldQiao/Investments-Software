from __future__ import annotations

from collections.abc import Iterable
from itertools import pairwise


def _date_key(value):
    return str(value)[:10]


def pair_returns(fund_rows: Iterable[dict], bench_rows: Iterable[dict]):
    """Return (date, fund return, benchmark return) for paired daily observations."""
    fund_by_date = {
        _date_key(row["date"]): row.get("daily_return")
        for row in fund_rows
        if row.get("daily_return") is not None
    }
    closes = sorted(
        (
            (_date_key(row["date"]), float(row["close"]))
            for row in bench_rows
            if row.get("close") is not None
        ),
        key=lambda item: item[0],
    )
    benchmark_by_date = {}
    for previous, current in pairwise(closes):
        if previous[1]:
            benchmark_by_date[current[0]] = current[1] / previous[1] - 1
    return [
        (float(fund_by_date[day]), benchmark_by_date[day])
        for day in sorted(fund_by_date)
        if day in benchmark_by_date
    ]


def beta(pairs):
    values = [(float(fund), float(benchmark)) for fund, benchmark in pairs]
    if len(values) < 2:
        return None
    fund_mean = sum(fund for fund, _ in values) / len(values)
    benchmark_mean = sum(benchmark for _, benchmark in values) / len(values)
    variance = sum((benchmark - benchmark_mean) ** 2 for _, benchmark in values)
    if not variance:
        return None
    covariance = sum(
        (fund - fund_mean) * (benchmark - benchmark_mean) for fund, benchmark in values
    )
    return covariance / variance
