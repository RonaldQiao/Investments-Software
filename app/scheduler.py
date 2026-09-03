from __future__ import annotations

import logging
from datetime import date, datetime, time
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


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in NYSE_HOLIDAYS


def run_job():
    conn = get_conn()
    if str(get_setting(conn, "snapshot_enabled", "1")) != "1":
        conn.close()
        return
    today = datetime.now(NY).date()
    if is_trading_day(today):
        take_snapshot(conn, today, "scheduled", refresh=True)
        LOGGER.info("Scheduled NAV snapshot written for %s", today)
    conn.close()


def catch_up(conn=None):
    own_conn = conn is None
    conn = conn or get_conn()
    now = datetime.now(NY)
    day = now.date()
    if not is_trading_day(day) or now.time() < time(16):
        day = day.fromordinal(day.toordinal() - 1)
        while not is_trading_day(day):
            day = day.fromordinal(day.toordinal() - 1)
    if _missing_snapshot(conn, day) and str(get_setting(conn, "snapshot_enabled", "1")) == "1":
        take_snapshot(conn, day, "scheduled", refresh=True)
        LOGGER.info("Catch-up NAV snapshot written for %s", day)
    if own_conn:
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
        await refresh_prices(conn)
        take_snapshot(conn, day, "scheduled", refresh=False)
        LOGGER.info("Catch-up NAV snapshot written for %s", day)
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
