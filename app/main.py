from __future__ import annotations

import os
import re

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import ACTIVE_DB, DEFAULT_FUND, fund_path, get_conn, init_db, list_funds
from .routes.api import router as api_router
from .routes.capital import router as capital_router
from .routes.dashboard import router as dashboard_router
from .routes.fees import router as fees_router
from .routes.funds import router as funds_router
from .routes.history import router as history_router
from .routes.pnl import router as pnl_router
from .routes.positions import router as positions_router
from .routes.settings import backup_database
from .routes.settings import router as settings_router
from .routes.trades import router as trades_router
from .routes.update import router as update_router
from .scheduler import catch_up_async
from .scheduler import start as start_scheduler
from .scheduler import stop as stop_scheduler

ROOT = os.path.dirname(__file__)
app = FastAPI(title="Ledger")
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")


@app.middleware("http")
async def select_fund(request, call_next):
    slug = request.cookies.get("fund", DEFAULT_FUND)
    if slug != DEFAULT_FUND and (
        not re.fullmatch(r"[a-z0-9-]+", slug) or not fund_path(slug).exists()
    ):
        slug = DEFAULT_FUND
    token = ACTIVE_DB.set(fund_path(slug))
    try:
        return await call_next(request)
    finally:
        ACTIVE_DB.reset(token)


app.include_router(dashboard_router)
app.include_router(update_router)
app.include_router(positions_router)
app.include_router(trades_router)
app.include_router(pnl_router)
app.include_router(capital_router)
app.include_router(fees_router)
app.include_router(history_router)
app.include_router(settings_router)
app.include_router(funds_router)
app.include_router(api_router)

__all__ = ["app", "backup_database"]


@app.on_event("startup")
async def startup():
    for fund in list_funds():
        conn = get_conn(fund["path"])
        try:
            init_db(conn)
        finally:
            conn.close()
    if os.environ.get("LEDGER_NO_SCHEDULER") != "1":
        await catch_up_async()
        start_scheduler()


@app.on_event("shutdown")
def shutdown():
    stop_scheduler()
