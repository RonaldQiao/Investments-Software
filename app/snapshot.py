from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import date

from .db import get_conn, init_db, list_funds
from .scheduler import run_snapshot_job


def _parser():
    parser = argparse.ArgumentParser(description="Write a Ledger NAV snapshot")
    parser.add_argument("--date", type=date.fromisoformat, help="Snapshot date (YYYY-MM-DD)")
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Write today's missing snapshot after 16:00 New York time",
    )
    parser.add_argument("--fund", help="Fund slug (default: all funds)")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    funds = list_funds()
    if args.fund:
        funds = [fund for fund in funds if fund["slug"] == args.fund]
        if not funds:
            print(f"snapshot failed: unknown fund {args.fund}", file=sys.stderr)
            return 1
    output = []
    for fund in funds:
        conn = get_conn(fund["path"])
        try:
            init_db(conn)
            result = asyncio.run(
                run_snapshot_job(
                    conn,
                    "cli",
                    snapshot_date=args.date,
                    catch_up=args.catch_up,
                    require_close=args.catch_up,
                )
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            print(f"snapshot failed: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        prefix = f"{fund['slug']} " if len(funds) > 1 else ""
        if result is None:
            output.append(f"{prefix}snapshot skipped")
            continue
        snapshot = result["snapshot"]
        daily_return = snapshot["daily_return"]
        rendered_return = "—" if daily_return is None else f"{daily_return:+.2%}"
        output.append(
            f"{prefix}snapshot {snapshot['date']} nav={snapshot['nav']:.2f} "
            f"navpu={snapshot['nav_per_unit']:.2f} ret={rendered_return}"
        )
    print("\n".join(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
