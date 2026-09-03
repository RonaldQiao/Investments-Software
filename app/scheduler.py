from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import holidays
from apscheduler.schedulers.background import BackgroundScheduler

from .db import get_conn, get_setting
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


def run_job():
    conn = get_conn()
    if str(get_setting(conn, "snapshot_enabled", "1")) != "1":
        conn.close()
        return
    today = datetime.now(NY).date()
    if is_trading_day(today):
        try:
            failures = asyncio.run(refresh_with_retries(conn))
            take_snapshot(conn, today, "scheduled", refresh=False)
            status = "partial" if failures else "ok"
            detail = ", ".join(failures)
            _write_job_log(conn, "scheduled", status, detail)
            LOGGER.info("Scheduled NAV snapshot written for %s", today)
        except Exception as exc:
            _write_job_log(conn, "scheduled", "failed", str(exc))
            LOGGER.exception("Scheduled NAV snapshot failed")
    conn.close()


async def catch_up_async():
    conn = get_conn()
    now = datetime.now(NY)
    day = now.date()
    if not is_trading_day(day) or now.time() < time(16):
        day = day.fromordinal(day.toordinal() - 1)
        while not is_trading_day(day):
            day = day.fromordinal(day.toordinal() - 1)
    if _missing_snapshot(conn, day) and str(get_setting(conn, "snapshot_enabled", "1")) == "1":
        try:
            failures = await refresh_with_retries(conn)
            take_snapshot(conn, day, "scheduled", refresh=False)
            _write_job_log(conn, "catch-up", "partial" if failures else "ok", ", ".join(failures))
            LOGGER.info("Catch-up NAV snapshot written for %s", day)
        except Exception as exc:
            _write_job_log(conn, "catch-up", "failed", str(exc))
            LOGGER.exception("Catch-up NAV snapshot failed")
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
