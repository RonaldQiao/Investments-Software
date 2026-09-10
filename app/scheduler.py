from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import holidays
from apscheduler.schedulers.background import BackgroundScheduler

from .db import get_conn, get_setting, list_funds
from .nav import take_snapshot
from .pricing import refresh_prices

LOGGER = logging.getLogger("ledger.scheduler")
NY = ZoneInfo("America/New_York")
NYSE_HOLIDAYS = holidays.financial_holidays("NYSE")
scheduler = BackgroundScheduler(timezone=NY)
RETRY_DELAY = 60


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in NYSE_HOLIDAYS


def _write_job_log(conn, job, status, detail=""):
    conn.execute(
        "INSERT INTO job_log(ts,job,status,detail) VALUES (?,?,?,?)",
        (datetime.now(NY).isoformat(), job, status, detail),
    )
    conn.commit()


async def refresh_with_retries(conn, sleep=asyncio.sleep):
    failures = []
    for attempt in range(3):
        failures = await refresh_prices(conn)
        if not failures:
            return []
        if attempt < 2:
            await sleep(RETRY_DELAY)
    return failures


async def run_snapshot_job(
    conn,
    job: str,
    snapshot_date: date | None = None,
    catch_up: bool = False,
    sleep=asyncio.sleep,
    require_close: bool = False,
):
    if str(get_setting(conn, "snapshot_enabled", "1")) != "1":
        return None
    now = datetime.now(NY)
    day = snapshot_date or now.date()
    if catch_up:
        if require_close and now.time() < time(16):
            return None
        if not require_close and now.time() < time(16):
            day = day.fromordinal(day.toordinal() - 1)
        while not is_trading_day(day):
            day = day.fromordinal(day.toordinal() - 1)
        if not _missing_snapshot(conn, day):
            return None
    elif snapshot_date is None and not is_trading_day(day):
        return None
    try:
        failures = await refresh_with_retries(conn, sleep=sleep)
        benchmark_close = None
        benchmark_symbol = str(get_setting(conn, "benchmark_symbol", "") or "").strip().upper()
        if benchmark_symbol:
            from .pricing import fetch_benchmark_close

            benchmark_close = await fetch_benchmark_close(benchmark_symbol, day)
        source = "cli" if job == "cli" else "scheduled"
        snapshot = take_snapshot(
            conn, day, source, refresh=False, benchmark_close=benchmark_close
        )
        _write_job_log(conn, job, "partial" if failures else "ok", ", ".join(failures))
        LOGGER.info("%s NAV snapshot written for %s", job.capitalize(), day)
        return {"snapshot": snapshot, "failures": failures}
    except Exception as exc:
        _write_job_log(conn, job, "failed", str(exc))
        LOGGER.exception("%s NAV snapshot failed", job.capitalize())
        raise


def run_job():
    for fund in list_funds():
        LOGGER.info("Running scheduled snapshot for fund %s", fund["slug"])
        conn = get_conn(fund["path"])
        try:
            asyncio.run(run_snapshot_job(conn, "scheduled"))
        finally:
            conn.close()


async def catch_up_async():
    for fund in list_funds():
        LOGGER.info("Running catch-up snapshot for fund %s", fund["slug"])
        conn = get_conn(fund["path"])
        try:
            await run_snapshot_job(conn, "catch-up", catch_up=True)
        finally:
            conn.close()


def _missing_snapshot(conn, day):
    return conn.execute("SELECT 1 FROM nav_snapshots WHERE date=?", (day.isoformat(),)).fetchone() is None


def start():
    if not scheduler.running:
        scheduler.add_job(run_job, "cron", hour=16, minute=0, day_of_week="mon-fri", id="eod-nav", replace_existing=True)
        scheduler.start()


def stop():
    if scheduler.running:
        scheduler.shutdown(wait=False)


def next_snapshot_label(conn):
    if str(get_setting(conn, "snapshot_enabled", "1")) != "1":
        return "disabled"
    job = scheduler.get_job("eod-nav")
    if job and job.next_run_time:
        return job.next_run_time.astimezone(NY).strftime("%a %Y-%m-%d %H:%M ET")
    moment = datetime.now(NY).replace(hour=16, minute=0, second=0, microsecond=0)
    if moment <= datetime.now(NY) or not is_trading_day(moment.date()):
        moment += timedelta(days=1)
        while not is_trading_day(moment.date()):
            moment += timedelta(days=1)
    return moment.strftime("%a %Y-%m-%d %H:%M ET")
