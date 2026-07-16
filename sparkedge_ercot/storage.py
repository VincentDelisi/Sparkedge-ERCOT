"""SQLite caching layer.

All external data is persisted here so the dashboard does not re-hit ERCOT /
EIA on every refresh. Each table uses an idempotent UPSERT keyed on the
natural grain of the data (interval + entity), so repeated pulls of overlapping
ranges are safe and cheap.

Design notes
------------
* Timestamps are stored as ISO-8601 UTC strings for portability and correct
  ordering. Helpers convert to/from tz-aware pandas Timestamps.
* Everything is defensive: a failed read returns an empty, correctly-typed
  DataFrame rather than raising, so the display layer can degrade gracefully.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterable

import pandas as pd

log = logging.getLogger(__name__)

# ISO in UTC, second resolution is plenty for these markets.
_ISO = "%Y-%m-%dT%H:%M:%S%z"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lmp (
    interval_start TEXT NOT NULL,   -- ISO-8601 UTC
    interval_end   TEXT,
    market         TEXT NOT NULL,   -- DAY_AHEAD_HOURLY | REAL_TIME_15_MIN
    hub            TEXT NOT NULL,   -- friendly label: Houston / North / West / South
    node           TEXT,
    lmp            REAL,
    energy         REAL,
    congestion     REAL,
    loss           REAL,
    PRIMARY KEY (interval_start, market, hub)
);

CREATE TABLE IF NOT EXISTS gas_price (
    interval_start TEXT NOT NULL,   -- ISO-8601 UTC
    source         TEXT NOT NULL,   -- 'eia'
    region         TEXT NOT NULL,   -- 'HENRY_HUB'
    price          REAL,            -- $/MMBtu
    PRIMARY KEY (interval_start, source, region)
);

CREATE TABLE IF NOT EXISTS load_actual (
    interval_start TEXT NOT NULL,
    interval_end   TEXT,
    load_mw        REAL,
    PRIMARY KEY (interval_start)
);

CREATE TABLE IF NOT EXISTS load_forecast (
    interval_start TEXT NOT NULL,
    interval_end   TEXT,
    load_mw        REAL,
    PRIMARY KEY (interval_start)
);

CREATE TABLE IF NOT EXISTS fuel_mix (
    interval_start TEXT NOT NULL,
    fuel           TEXT NOT NULL,
    mw             REAL,
    PRIMARY KEY (interval_start, fuel)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_lmp_hub_mkt ON lmp (hub, market, interval_start);
CREATE INDEX IF NOT EXISTS idx_gas_region ON gas_price (region, source, interval_start);
CREATE INDEX IF NOT EXISTS idx_fuelmix_int ON fuel_mix (interval_start);
"""


class Storage:
    """Thin, thread-safe wrapper over a SQLite cache file."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------ #
    # connection helpers
    # ------------------------------------------------------------------ #
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ #
    # write helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _iso(ts) -> str | None:
        if ts is None or pd.isna(ts):
            return None
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC").strftime(_ISO)

    def upsert_lmp(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        sql = """
        INSERT INTO lmp (interval_start, interval_end, market, hub, node,
                         lmp, energy, congestion, loss)
        VALUES (:interval_start, :interval_end, :market, :hub, :node,
                :lmp, :energy, :congestion, :loss)
        ON CONFLICT(interval_start, market, hub) DO UPDATE SET
            lmp=excluded.lmp, energy=excluded.energy,
            congestion=excluded.congestion, loss=excluded.loss,
            interval_end=excluded.interval_end, node=excluded.node;
        """
        with self._lock, self._conn() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def upsert_gas(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        sql = """
        INSERT INTO gas_price (interval_start, source, region, price)
        VALUES (:interval_start, :source, :region, :price)
        ON CONFLICT(interval_start, source, region) DO UPDATE SET
            price=excluded.price;
        """
        with self._lock, self._conn() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def upsert_load(self, rows: Iterable[dict], forecast: bool = False) -> int:
        rows = list(rows)
        if not rows:
            return 0
        table = "load_forecast" if forecast else "load_actual"
        sql = f"""
        INSERT INTO {table} (interval_start, interval_end, load_mw)
        VALUES (:interval_start, :interval_end, :load_mw)
        ON CONFLICT(interval_start) DO UPDATE SET
            load_mw=excluded.load_mw, interval_end=excluded.interval_end;
        """
        with self._lock, self._conn() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def upsert_fuel_mix(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        sql = """
        INSERT INTO fuel_mix (interval_start, fuel, mw)
        VALUES (:interval_start, :fuel, :mw)
        ON CONFLICT(interval_start, fuel) DO UPDATE SET mw=excluded.mw;
        """
        with self._lock, self._conn() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock, self._conn() as conn:
            cur = conn.execute("SELECT value FROM meta WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------ #
    # read helpers -- always return tz-aware, sorted DataFrames
    # ------------------------------------------------------------------ #
    def _read(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        try:
            with self._lock, self._conn() as conn:
                df = pd.read_sql_query(sql, conn, params=params)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("storage read failed: %s", exc)
            return pd.DataFrame()
        for col in ("interval_start", "interval_end"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
        return df

    def read_lmp(
        self,
        market: str | None = None,
        hub: str | None = None,
        start: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        clauses, params = [], []
        if market:
            clauses.append("market=?"); params.append(market)
        if hub:
            clauses.append("hub=?"); params.append(hub)
        if start is not None:
            clauses.append("interval_start>=?"); params.append(self._iso(start))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._read(
            f"SELECT * FROM lmp {where} ORDER BY interval_start", tuple(params)
        )

    def read_gas(
        self,
        source: str | None = None,
        region: str | None = None,
        start: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        clauses, params = [], []
        if source:
            clauses.append("source=?"); params.append(source)
        if region:
            clauses.append("region=?"); params.append(region)
        if start is not None:
            clauses.append("interval_start>=?"); params.append(self._iso(start))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._read(
            f"SELECT * FROM gas_price {where} ORDER BY interval_start",
            tuple(params),
        )

    def read_load(self, forecast: bool = False,
                  start: str | pd.Timestamp | None = None) -> pd.DataFrame:
        table = "load_forecast" if forecast else "load_actual"
        clauses, params = [], []
        if start is not None:
            clauses.append("interval_start>=?"); params.append(self._iso(start))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._read(
            f"SELECT * FROM {table} {where} ORDER BY interval_start", tuple(params)
        )

    def read_fuel_mix(self, start: str | pd.Timestamp | None = None) -> pd.DataFrame:
        clauses, params = [], []
        if start is not None:
            clauses.append("interval_start>=?"); params.append(self._iso(start))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._read(
            f"SELECT * FROM fuel_mix {where} ORDER BY interval_start", tuple(params)
        )

    def coverage(self) -> dict[str, int]:
        """Row counts per table -- handy for the diagnostics footer."""
        out: dict[str, int] = {}
        tables = ["lmp", "gas_price", "load_actual", "load_forecast", "fuel_mix"]
        try:
            with self._lock, self._conn() as conn:
                for t in tables:
                    cur = conn.execute(f"SELECT COUNT(*) FROM {t}")
                    out[t] = cur.fetchone()[0]
        except Exception as exc:  # pragma: no cover
            log.warning("coverage failed: %s", exc)
        return out
