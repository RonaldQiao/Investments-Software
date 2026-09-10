from __future__ import annotations

import os
import re
import sqlite3
from contextvars import ContextVar
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LEDGER_DB", ROOT / "data" / "ledger.db"))
FUNDS_DIR = DB_PATH.parent / "funds"
DEFAULT_FUND = "ledger"
ACTIVE_DB: ContextVar[Path | None] = ContextVar("active_db", default=None)


def fund_path(slug: str) -> Path:
    return DB_PATH if slug == DEFAULT_FUND else FUNDS_DIR / f"{slug}.db"


def get_conn(path: Path | None = None) -> sqlite3.Connection:
    database_path = path or ACTIVE_DB.get() or DB_PATH
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    if not slug or slug == DEFAULT_FUND or fund_path(slug).exists():
        raise ValueError("Fund name is empty or already exists")
    return slug


def list_funds() -> list[dict]:
    paths = [DB_PATH]
    if FUNDS_DIR.exists():
        paths.extend(sorted(FUNDS_DIR.glob("*.db"), key=lambda path: path.stem))
    active_path = ACTIVE_DB.get() or DB_PATH
    funds = []
    for path in paths:
        conn = None
        try:
            conn = get_conn(path)
            has_settings = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
            ).fetchone()
            if not has_settings:
                init_db(conn)
            name = get_setting(conn, "fund_name", path.stem)
            funds.append(
                {
                    "slug": DEFAULT_FUND if path == DB_PATH else path.stem,
                    "name": name,
                    "path": path,
                    "relative_path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                    "active": path == active_path,
                }
            )
        except (OSError, sqlite3.Error):
            pass
        finally:
            if conn is not None:
                conn.close()
    return funds


def create_fund(name: str) -> str:
    slug = slugify(name)
    FUNDS_DIR.mkdir(parents=True, exist_ok=True)
    path = fund_path(slug)
    if path.exists():
        raise ValueError("Fund name is empty or already exists")
    conn = get_conn(path)
    try:
        init_db(conn)
        set_setting(conn, "fund_name", name.strip())
    finally:
        conn.close()
    return slug


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    conn = conn or get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS instruments (
          id INTEGER PRIMARY KEY,
          symbol TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          asset_class TEXT NOT NULL,
          currency TEXT NOT NULL DEFAULT 'USD',
          multiplier REAL NOT NULL DEFAULT 1,
          pricing_source TEXT NOT NULL DEFAULT 'yahoo',
          yahoo_symbol TEXT,
          manual_mark REAL,
          manual_mark_at TEXT,
          price_scale REAL NOT NULL DEFAULT 1,
          notes TEXT,
          underlying TEXT,
          expiry TEXT,
          strike REAL,
          option_type TEXT CHECK(option_type IN ('C','P'))
        );
        CREATE TABLE IF NOT EXISTS trades (
          id INTEGER PRIMARY KEY,
          instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
          ts TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
          quantity REAL NOT NULL CHECK(quantity > 0),
          price REAL NOT NULL,
          fees REAL NOT NULL DEFAULT 0,
          fx_rate REAL NOT NULL DEFAULT 1,
          notes TEXT
        );
        CREATE TABLE IF NOT EXISTS prices (
          instrument_id INTEGER PRIMARY KEY REFERENCES instruments(id) ON DELETE CASCADE,
          price REAL NOT NULL,
          ts TEXT NOT NULL,
          source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cash_flows (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          amount REAL NOT NULL,
          lp_id INTEGER REFERENCES lps(id) ON DELETE SET NULL,
          note TEXT
        );
        CREATE TABLE IF NOT EXISTS cash_adjustments (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          amount REAL NOT NULL,
          category TEXT NOT NULL DEFAULT 'other',
          note TEXT
        );
        CREATE TABLE IF NOT EXISTS lps (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          is_gp INTEGER NOT NULL DEFAULT 0,
          mgmt_fee_bps REAL,
          perf_fee_pct REAL,
          hwm REAL
        );
        CREATE TABLE IF NOT EXISTS lp_units (
          id INTEGER PRIMARY KEY,
          lp_id INTEGER NOT NULL REFERENCES lps(id) ON DELETE CASCADE,
          ts TEXT NOT NULL,
          units REAL NOT NULL,
          nav_per_unit REAL NOT NULL,
          cash_flow_id INTEGER REFERENCES cash_flows(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS nav_snapshots (
          date TEXT PRIMARY KEY,
          ts TEXT NOT NULL,
          nav REAL NOT NULL,
          cash REAL NOT NULL,
          gross_long REAL NOT NULL,
          gross_short REAL NOT NULL,
          net_exposure REAL NOT NULL,
          flows_today REAL NOT NULL DEFAULT 0,
          units_outstanding REAL NOT NULL DEFAULT 0,
          nav_per_unit REAL NOT NULL DEFAULT 0,
          daily_return REAL,
          levered_return REAL,
          mgmt_fee_accrued REAL NOT NULL DEFAULT 0,
          source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshot_marks (
          date TEXT NOT NULL REFERENCES nav_snapshots(date) ON DELETE CASCADE,
          instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
          mark REAL NOT NULL,
          fx_rate REAL NOT NULL DEFAULT 1,
          source TEXT NOT NULL,
          PRIMARY KEY (date, instrument_id)
        );
        CREATE TABLE IF NOT EXISTS fee_events (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('mgmt','perf','settle')),
          amount REAL NOT NULL,
          hwm_before REAL,
          hwm_after REAL,
          note TEXT,
          lp_id INTEGER REFERENCES lps(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS lp_fee_accruals (
          lp_id INTEGER NOT NULL REFERENCES lps(id) ON DELETE CASCADE,
          date TEXT NOT NULL,
          mgmt REAL NOT NULL DEFAULT 0,
          perf REAL NOT NULL DEFAULT 0,
          PRIMARY KEY (lp_id, date)
        );
        CREATE TABLE IF NOT EXISTS job_log (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          job TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('ok','partial','failed')),
          detail TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS benchmark_closes (
          symbol TEXT NOT NULL,
          date TEXT NOT NULL,
          close REAL,
          PRIMARY KEY (symbol, date)
        );
        CREATE TABLE IF NOT EXISTS fx_rates (
          currency TEXT PRIMARY KEY,
          rate REAL NOT NULL,
          ts TEXT NOT NULL,
          source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(instruments)").fetchall()
    }
    for name, definition in (
        ("underlying", "TEXT"),
        ("expiry", "TEXT"),
        ("strike", "REAL"),
        ("option_type", "TEXT CHECK(option_type IN ('C','P'))"),
        ("price_scale", "REAL NOT NULL DEFAULT 1"),
    ):
        if name not in existing_columns:
            conn.execute(f"ALTER TABLE instruments ADD COLUMN {name} {definition}")
    trade_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
    }
    if "fx_rate" not in trade_columns:
        conn.execute("ALTER TABLE trades ADD COLUMN fx_rate REAL NOT NULL DEFAULT 1")
    snapshot_mark_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(snapshot_marks)").fetchall()
    }
    if "fx_rate" not in snapshot_mark_columns:
        conn.execute(
            "ALTER TABLE snapshot_marks ADD COLUMN fx_rate REAL NOT NULL DEFAULT 1"
        )
    lp_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(lps)").fetchall()
    }
    for name, definition in (
        ("mgmt_fee_bps", "REAL"),
        ("perf_fee_pct", "REAL"),
        ("hwm", "REAL"),
    ):
        if name not in lp_columns:
            conn.execute(f"ALTER TABLE lps ADD COLUMN {name} {definition}")
    fee_event_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(fee_events)").fetchall()
    }
    if "lp_id" not in fee_event_columns:
        conn.execute("ALTER TABLE fee_events ADD COLUMN lp_id INTEGER REFERENCES lps(id)")
    defaults = {
        "leverage": "1.0",
        "borrow_rate": "0.05",
        "fund_name": "Ledger",
        "mgmt_fee_bps": "200",
        "perf_fee_pct": "20",
        "hwm_per_unit": "1000",
        "inception_nav_per_unit": "1000",
        "snapshot_enabled": "1",
        "last_refresh_failures": "[]",
        "last_refresh_at": "",
        "benchmark_symbol": "SPY",
        "base_currency": "USD",
    }
    conn.executemany(
        "INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)",
        defaults.items(),
    )
    conn.execute("INSERT OR IGNORE INTO lps(name,is_gp) VALUES ('Principal',0)")
    conn.commit()
    if own_conn:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
