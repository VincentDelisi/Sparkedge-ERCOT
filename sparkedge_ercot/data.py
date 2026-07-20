"""Data-acquisition layer.

Pulls everything Sparkedge ERCOT needs and writes it into the SQLite cache:

* ERCOT day-ahead hourly + real-time 15-min SPP for the trading hubs (gridstatus)
* ERCOT load, load forecast, fuel mix                               (gridstatus)
* EIA Henry Hub daily spot natural-gas price                        (EIA v2 API)
* Waha (Permian) daily natural-gas price, for HB_WEST only          (OilPriceAPI)

Every network call is wrapped so that a failure is *logged and swallowed*: the
fetchers return the number of rows written (0 on failure) and never raise into
the caller. This is what lets the Streamlit app "degrade, not crash" -- if a
source is down the app simply serves whatever is already cached.

ERCOT has no per-hub gas feed. Most hubs use EIA Henry Hub as their reference
gas; HB_WEST instead uses real Waha (Permian) gas from OilPriceAPI, since Waha
trades on a deep, volatile discount to Henry Hub. We keep modest inter-call
sleeps and retries so a flaky pull doesn't abort a run.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from .config import HUBS, SETTINGS, Settings
from .storage import Storage

log = logging.getLogger(__name__)

# gridstatus is imported lazily inside the fetcher so that importing this module
# (e.g. for tests, or when only reading the cache) never requires the network
# stack or the heavy dependency chain to succeed.


# --------------------------------------------------------------------------- #
# retry / rate-limit helpers
# --------------------------------------------------------------------------- #
def _with_retries(fn: Callable, *, label: str, settings: Settings):
    """Run ``fn`` with retries; return its result or None on total failure."""
    last_exc = None
    for attempt in range(1, settings.max_retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we intentionally catch all
            last_exc = exc
            wait = settings.retry_backoff_seconds * attempt
            log.warning(
                "[%s] attempt %d/%d failed: %s -- retrying in %.0fs",
                label, attempt, settings.max_retries, exc, wait,
            )
            time.sleep(wait)
    log.error("[%s] giving up after %d attempts: %s",
              label, settings.max_retries, last_exc)
    return None


def _iso(ts) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S%z")


class DataService:
    """Fetches from external sources and persists into the cache."""

    def __init__(self, storage: Storage, settings: Settings = SETTINGS):
        self.storage = storage
        self.settings = settings
        self._iso = None  # gridstatus ERCOT handle, created lazily

    # ------------------------------------------------------------------ #
    # gridstatus handle
    # ------------------------------------------------------------------ #
    def _ercot(self):
        if self._iso is None:
            # Apply the optional SSL relaxation before any request is made
            # (needed for load/fuel-mix on TLS-intercepting networks).
            from . import ssl_compat
            ssl_compat.install(self.settings.insecure_ssl_caiso)

            from gridstatus import Ercot  # lazy import
            self._iso = Ercot()
        return self._iso

    def _sleep(self):
        time.sleep(self.settings.inter_call_sleep_seconds)

    # ------------------------------------------------------------------ #
    # LMP (ERCOT SPP)
    # ------------------------------------------------------------------ #
    def fetch_lmp(self, market_name: str, start, end=None) -> int:
        """market_name in {'DAY_AHEAD_HOURLY','REAL_TIME_15_MIN'}."""

        def _do():
            return self._ercot().get_spp(
                date=start,
                end=end,
                market=market_name,
                location_type="Trading Hub",
            )

        df = _with_retries(_do, label=f"lmp:{market_name}", settings=self.settings)
        self._sleep()
        if df is None or df.empty:
            return 0

        node_to_label = {h.node: h.label for h in HUBS}
        rows = []
        for _, r in df.iterrows():
            node = r.get("Location")
            label = node_to_label.get(node)
            if label is None:
                continue  # skip HUBAVG / BUSAVG / PAN and any non-configured hub
            rows.append({
                "interval_start": _iso(r["Interval Start"]),
                "interval_end": _iso(r.get("Interval End")),
                "market": market_name,
                "hub": label,
                "node": node,
                "lmp": _f(r.get("SPP")),
                "energy": None,
                "congestion": None,
                "loss": None,
            })
        return self.storage.upsert_lmp(rows)

    # ------------------------------------------------------------------ #
    # Load / forecast / fuel mix
    # ------------------------------------------------------------------ #
    def fetch_load(self, start, end=None) -> int:
        def _do():
            return self._ercot().get_load(date=start, end=end)

        df = _with_retries(_do, label="load", settings=self.settings)
        self._sleep()
        if df is None or df.empty:
            return 0
        rows = [{
            "interval_start": _iso(r["Interval Start"]),
            "interval_end": _iso(r.get("Interval End")),
            "load_mw": _f(r.get("Load")),
        } for _, r in df.iterrows()]
        return self.storage.upsert_load(rows, forecast=False)

    def fetch_load_forecast(self, start, end=None) -> int:
        def _do():
            return self._ercot().get_load_forecast(date=start, end=end)

        df = _with_retries(_do, label="load_forecast", settings=self.settings)
        self._sleep()
        if df is None or df.empty:
            return 0
        # ERCOT's forecast is published per forecast zone; "System Total" is the
        # aggregate we want. Fall back to a generic single-load column if the
        # report shape changes.
        load_col = _pick(df, ["System Total", "Load Forecast", "Load", "MW"])
        rows = [{
            "interval_start": _iso(r["Interval Start"]),
            "interval_end": _iso(r.get("Interval End")),
            "load_mw": _f(r.get(load_col)) if load_col else None,
        } for _, r in df.iterrows()]
        return self.storage.upsert_load(rows, forecast=True)

    def fetch_fuel_mix(self, start, end=None) -> int:
        # ERCOT get_fuel_mix only accepts a single date (no end range).
        def _do():
            return self._ercot().get_fuel_mix(date=start)

        df = _with_retries(_do, label="fuel_mix", settings=self.settings)
        self._sleep()
        if df is None or df.empty:
            return 0
        # ERCOT fuel mix carries the timestamp in "Time" (no "Interval Start").
        ts_col = "Interval Start" if "Interval Start" in df.columns else "Time"
        skip = {"Time", "Interval Start", "Interval End"}
        fuel_cols = [c for c in df.columns if c not in skip]
        rows = []
        for _, r in df.iterrows():
            istart = _iso(r[ts_col])
            for fuel in fuel_cols:
                rows.append({
                    "interval_start": istart,
                    "fuel": fuel,
                    "mw": _f(r.get(fuel)),
                })
        return self.storage.upsert_fuel_mix(rows)

    # ------------------------------------------------------------------ #
    # Gas -- EIA Henry Hub daily spot (ERCOT's reference gas for all hubs)
    # ------------------------------------------------------------------ #
    def fetch_gas_eia(self, start: date, end: date | None = None) -> int:
        key = self.settings.eia_api_key
        if not key:
            log.info("EIA_API_KEY not set -- skipping Henry Hub pull "
                     "(implied heat rates will be n/a until a key is provided).")
            return 0

        import requests  # local import keeps module import cheap

        end = end or date.today()
        params = {
            "api_key": key,
            "frequency": "daily",
            "data[0]": "value",
            "facets[series][]": self.settings.eia_henry_hub_series,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": 5000,
        }
        url = f"https://api.eia.gov/v2/{self.settings.eia_route}/data/"

        def _do():
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()

        payload = _with_retries(_do, label="gas:eia", settings=self.settings)
        if not payload:
            return 0
        records = (payload.get("response") or {}).get("data") or []
        rows = []
        for rec in records:
            period = rec.get("period")
            val = rec.get("value")
            if period is None or val is None:
                continue
            # daily periods are dates; store at UTC midnight
            ts = pd.Timestamp(period, tz="UTC")
            rows.append({
                "interval_start": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "source": "eia",
                "region": "HENRY_HUB",
                "price": _f(val),
            })
        return self.storage.upsert_gas(rows)

    # ------------------------------------------------------------------ #
    # Gas -- OilPriceAPI Waha (Permian gas, reference for HB_WEST only)
    # ------------------------------------------------------------------ #
    def fetch_gas_waha(self) -> int:
        """Pull the latest Waha (Permian) gas price from OilPriceAPI.

        OilPriceAPI's free tier is a daily-latest quota (200 req/month, 10/min
        after the 7-day trial), so this stores exactly ONE row per call: the
        latest Waha quote, keyed at today's UTC midnight. Historical backfill
        via the /v1/prices/historical endpoint is attempted best-effort -- it
        is NOT guaranteed on the free tier, so any failure there is caught and
        swallowed; the graceful Waha-history fallback in
        ``compute.Analytics._gas_for_hub`` covers the resulting gaps.

        Returns the number of rows written (0 if no key or the pull failed).
        """
        key = self.settings.oilpriceapi_key
        if not key:
            log.info("OILPRICEAPI_KEY not set -- skipping Waha pull "
                      "(HB_WEST implied heat rate will fall back to Henry Hub "
                      "until a key is provided).")
            return 0

        import requests  # local import keeps module import cheap

        headers = {"Authorization": f"Token {key}"}
        latest_url = "https://api.oilpriceapi.com/v1/prices/latest"
        params = {"by_code": self.settings.oilpriceapi_waha_code}

        def _do_latest():
            resp = requests.get(latest_url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()

        payload = _with_retries(_do_latest, label="gas:waha_latest", settings=self.settings)
        rows_written = 0
        if payload:
            data = payload.get("data") or payload.get("result") or payload
            price = None
            if isinstance(data, dict):
                price = data.get("price") or data.get("value")
            if price is not None:
                today = pd.Timestamp.now(tz="UTC").normalize()
                rows = [{
                    "interval_start": today.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "source": "oilpriceapi",
                    "region": "WAHA",
                    "price": _f(price),
                }]
                rows_written += self.storage.upsert_gas(rows)
            else:
                log.warning("[gas:waha_latest] response had no parsable price: %s", payload)
        else:
            log.warning("[gas:waha_latest] no data returned -- HB_WEST gas will rely on "
                        "cached history / Henry Hub fallback.")

        # Optional history: free-tier availability is not guaranteed, so this is
        # strictly best-effort -- any failure here must not fail the refresh.
        try:
            hist_url = "https://api.oilpriceapi.com/v1/prices/historical"
            end = date.today()
            start = end - timedelta(days=self.settings.backfill_days)
            hist_params = {
                "by_code": self.settings.oilpriceapi_waha_code,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "interval": "day",
            }

            def _do_hist():
                resp = requests.get(hist_url, params=hist_params, headers=headers, timeout=30)
                resp.raise_for_status()
                return resp.json()

            hist_payload = _with_retries(_do_hist, label="gas:waha_history", settings=self.settings)
            if hist_payload:
                records = (
                    hist_payload.get("data")
                    or hist_payload.get("prices")
                    or hist_payload.get("result")
                    or []
                )
                if isinstance(records, list) and records:
                    hist_rows = []
                    for rec in records:
                        if not isinstance(rec, dict):
                            continue
                        period = rec.get("date") or rec.get("period") or rec.get("created_at")
                        val = rec.get("price") or rec.get("value")
                        if period is None or val is None:
                            continue
                        ts = pd.Timestamp(period, tz="UTC").normalize()
                        hist_rows.append({
                            "interval_start": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "source": "oilpriceapi",
                            "region": "WAHA",
                            "price": _f(val),
                        })
                    if hist_rows:
                        rows_written += self.storage.upsert_gas(hist_rows)
                        log.info("[gas:waha_history] upserted %d historical rows", len(hist_rows))
        except Exception as exc:  # noqa: BLE001 - optional path, never fail the refresh
            log.info("[gas:waha_history] unavailable on this plan/endpoint (%s) -- "
                     "relying on latest-only + basis fallback in compute.py.", exc)

        return rows_written

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def refresh_today(self) -> dict[str, int]:
        """Pull today's data for a live refresh. Returns per-source row counts."""
        today = pd.Timestamp.now(tz="US/Central").normalize()
        results: dict[str, int] = {}
        results["lmp_dam"] = self.fetch_lmp("DAY_AHEAD_HOURLY", today)
        results["lmp_rt"] = self.fetch_lmp("REAL_TIME_15_MIN", today)
        results["load"] = self.fetch_load(today)
        results["load_forecast"] = self.fetch_load_forecast(today)
        results["fuel_mix"] = self.fetch_fuel_mix(today)
        results["gas_eia"] = self.fetch_gas_eia(date.today() - timedelta(days=7))
        results["gas_waha"] = self.fetch_gas_waha()
        self.storage.set_meta("last_refresh", pd.Timestamp.utcnow().isoformat())
        log.info("refresh_today complete: %s", results)
        return results

    def backfill(self, days: int | None = None) -> dict[str, int]:
        """Pull ``days`` of history to seed the rolling window & charts.

        Pulls are chunked day-by-day for LMP/load/fuel-mix so a single failing
        day does not abort the whole backfill.
        """
        days = days or self.settings.backfill_days
        end = pd.Timestamp.now(tz="US/Central").normalize()
        start = end - pd.Timedelta(days=days)
        totals: dict[str, int] = {
            "lmp_dam": 0, "lmp_rt": 0, "load": 0,
            "load_forecast": 0, "fuel_mix": 0, "gas_eia": 0, "gas_waha": 0,
        }

        # Gas via EIA can be pulled as a range (cheap, daily grain).
        totals["gas_eia"] += self.fetch_gas_eia(start.date(), end.date())
        # Waha (OilPriceAPI) is a single daily-latest pull on the free tier;
        # fetch_gas_waha() also makes a best-effort attempt at history so a
        # backfill gets as much real Waha coverage as the plan allows.
        totals["gas_waha"] += self.fetch_gas_waha()

        # Day-ahead LMP + load + fuel mix: iterate day by day.
        cur = start
        while cur < end:
            nxt = cur + pd.Timedelta(days=1)
            log.info("backfill day %s", cur.date())
            totals["lmp_dam"] += self.fetch_lmp("DAY_AHEAD_HOURLY", cur, nxt)
            totals["load"] += self.fetch_load(cur, nxt)
            totals["load_forecast"] += self.fetch_load_forecast(cur, nxt)
            totals["fuel_mix"] += self.fetch_fuel_mix(cur, nxt)
            cur = nxt

        # Real-time 15-min is large; only backfill the most recent 7 days of it
        # so charts have intraday detail without an excessive number of pulls.
        rt_start = max(start, end - pd.Timedelta(days=7))
        cur = rt_start
        while cur < end:
            nxt = cur + pd.Timedelta(days=1)
            totals["lmp_rt"] += self.fetch_lmp("REAL_TIME_15_MIN", cur, nxt)
            cur = nxt

        self.storage.set_meta("last_backfill", pd.Timestamp.utcnow().isoformat())
        self.storage.set_meta("backfill_days", str(days))
        log.info("backfill complete: %s", totals)
        return totals


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
def _f(v):
    """Coerce to float, tolerating None/NaN/strings."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None
