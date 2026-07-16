"""Command-line entrypoint for Sparkedge ERCOT.

Examples
--------
    # Seed the cache with ~45 days of history (needed for the 30d rolling band)
    python -m sparkedge_ercot --backfill

    # Backfill a custom horizon
    python -m sparkedge_ercot --backfill --days 60

    # One-shot live refresh of today's data
    python -m sparkedge_ercot --refresh

    # Print cache coverage / diagnostics
    python -m sparkedge_ercot --status

    # Launch the Streamlit dashboard (thin wrapper around `streamlit run`)
    python -m sparkedge_ercot --serve

All commands degrade gracefully: a failed source is logged and skipped, never
fatal.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from .config import SETTINGS
from .storage import Storage
from .data import DataService


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # gridstatus is chatty at DEBUG; keep it at INFO unless -vv.
    if not verbose:
        logging.getLogger("gridstatus").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sparkedge_ercot",
        description="Implied heat rate & spark spread monitor for the ERCOT (Texas) power market.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--backfill", action="store_true",
                   help="Pull historical data to seed the cache and rolling window.")
    g.add_argument("--refresh", action="store_true",
                   help="Pull today's latest data (live refresh).")
    g.add_argument("--status", action="store_true",
                   help="Print cache coverage and last-refresh diagnostics.")
    g.add_argument("--serve", action="store_true",
                   help="Launch the Streamlit dashboard.")
    p.add_argument("--days", type=int, default=None,
                   help="Backfill horizon in days (default: config backfill_days).")
    p.add_argument("--db", type=str, default=None,
                   help="Override the SQLite cache path.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose (DEBUG) logging.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    log = logging.getLogger("sparkedge_ercot.cli")

    if args.db:
        SETTINGS.db_path = args.db

    # --serve doesn't need the DataService / network stack.
    if args.serve:
        app = Path(__file__).with_name("app.py")
        log.info("launching Streamlit app: %s", app)
        return subprocess.call(
            [sys.executable, "-m", "streamlit", "run", str(app)]
        )

    storage = Storage(SETTINGS.db_path)

    if args.status:
        cov = storage.coverage()
        print("Sparkedge ERCOT -- cache status")
        print("  db path       :", SETTINGS.db_path)
        print("  last refresh  :", storage.get_meta("last_refresh"))
        print("  last backfill :", storage.get_meta("last_backfill"))
        print("  rows:")
        for tbl, n in cov.items():
            print(f"    {tbl:<14}: {n:,}")
        eia = "set" if SETTINGS.eia_api_key else "NOT set (Henry Hub disabled)"
        print("  EIA_API_KEY   :", eia)
        return 0

    service = DataService(storage, SETTINGS)

    if args.backfill:
        log.info("starting backfill (days=%s) ...", args.days or SETTINGS.backfill_days)
        totals = service.backfill(days=args.days)
        print("Backfill complete. Rows written:")
        for k, v in totals.items():
            print(f"  {k:<14}: {v:,}")
        return 0

    if args.refresh:
        log.info("starting live refresh ...")
        totals = service.refresh_today()
        print("Refresh complete. Rows written:")
        for k, v in totals.items():
            print(f"  {k:<14}: {v:,}")
        return 0

    # No action flag -> show help.
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
