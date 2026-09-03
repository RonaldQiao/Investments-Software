from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LEDGER_DB", ROOT / "data" / "ledger.db"))


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
          notes TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
          id INTEGER PRIMARY KEY,
          instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
          ts TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
          quantity REAL NOT NULL CHECK(quantity > 0),
          price REAL NOT NULL,
          fees REAL NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS lps (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          is_gp INTEGER NOT NULL DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS fee_events (
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('mgmt','perf','settle')),
          amount REAL NOT NULL,
          hwm_before REAL,
          hwm_after REAL,
          note TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    defaults = {
        "leverage": "1.0",
        "borrow_rate": "0.05",
        "fund_name": "Ledger",
        "mgmt_fee_bps": "200",
        "perf_fee_pct": "20",
        "hwm_per_unit": "1000",
        "inception_nav_per_unit": "1000",
        "snapshot_enabled": "1",
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
