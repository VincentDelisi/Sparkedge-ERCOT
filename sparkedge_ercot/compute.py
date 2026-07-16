"""Compute layer: implied heat rates, spark spreads, rolling stats, alerts.

Reads from the SQLite cache (via Storage) and returns tidy pandas DataFrames the
display layer can render directly. No network access happens here.

Definitions
-----------
Implied market heat rate (per interval, per hub):
    IHR = power_price / gas_price                     [MMBtu/MWh]

Spark spread for a unit of a given heat rate HR:
    spark = power_price - (HR * gas_price) - VOM       [$/MWh]
A unit is "in the money" when spark > 0. Equivalently, a unit is in the money
whenever the *implied* market heat rate exceeds the unit's own heat rate (before
VOM); the VOM term shifts that breakeven slightly.

Rolling statistics (per hub):
    mean_t, std_t = trailing {window}-day mean/std of IHR
    z_t           = (IHR_t - mean_t) / std_t
    dislocation   = |z_t| > sigma_threshold
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import HUBS_BY_LABEL, SETTINGS, UNIT_CLASSES, Settings
from .storage import Storage

log = logging.getLogger(__name__)


class Analytics:
    def __init__(self, storage: Storage, settings: Settings = SETTINGS):
        self.storage = storage
        self.settings = settings

    # ------------------------------------------------------------------ #
    # gas price series -- one representative $/MMBtu series per hub
    # ------------------------------------------------------------------ #
    def _gas_for_hub(self, hub_label: str, start=None) -> pd.DataFrame:
        """Return a daily gas price series (columns: date, gas_price) for a hub.

        ERCOT has no per-hub gas feed, so every hub uses the same reference gas:
        the EIA Henry Hub daily spot price. If no EIA gas is cached (e.g. no
        EIA_API_KEY was set), this returns an empty frame and implied heat rates
        degrade to n/a rather than crashing.
        """
        hub = HUBS_BY_LABEL.get(hub_label)
        if hub is None:
            return pd.DataFrame(columns=["date", "gas_price"])

        g = self.storage.read_gas(source="eia", region="HENRY_HUB", start=start)
        if g.empty:
            return pd.DataFrame(columns=["date", "gas_price"])
        g = g.dropna(subset=["price"]).copy()
        g["date"] = g["interval_start"].dt.tz_convert("US/Central").dt.normalize()
        return (g.groupby("date", as_index=False)["price"]
                 .mean().rename(columns={"price": "gas_price"}))

    # ------------------------------------------------------------------ #
    # implied heat rate time series per hub
    # ------------------------------------------------------------------ #
    def heat_rate_series(
        self,
        market: str = "DAY_AHEAD_HOURLY",
        start=None,
    ) -> pd.DataFrame:
        """Tidy long frame: interval_start, hub, lmp, gas_price, implied_hr,
        plus rolling mean/std/zscore/dislocation flags per hub.
        """
        lmp = self.storage.read_lmp(market=market, start=start)
        if lmp.empty:
            return pd.DataFrame(columns=[
                "interval_start", "hub", "lmp", "gas_price", "implied_hr",
                "hr_mean", "hr_std", "hr_z", "dislocation", "upper", "lower",
            ])

        frames = []
        for hub_label, sub in lmp.groupby("hub"):
            sub = sub.dropna(subset=["lmp"]).copy()
            if sub.empty:
                continue
            gas = self._gas_for_hub(hub_label, start=start)
            sub["date"] = sub["interval_start"].dt.tz_convert("US/Central").dt.normalize()
            if gas.empty:
                sub["gas_price"] = np.nan
            else:
                sub = sub.merge(gas, on="date", how="left")
            sub["implied_hr"] = sub["lmp"] / sub["gas_price"].replace(0, np.nan)
            frames.append(sub)

        if not frames:
            return pd.DataFrame()

        out = pd.concat(frames, ignore_index=True)
        out = out.sort_values(["hub", "interval_start"]).reset_index(drop=True)
        out = self._add_rolling_stats(out)
        cols = ["interval_start", "hub", "lmp", "gas_price", "implied_hr",
                "hr_mean", "hr_std", "hr_z", "dislocation", "upper", "lower"]
        return out[cols]

    def _add_rolling_stats(self, df: pd.DataFrame) -> pd.DataFrame:
        window = f"{self.settings.rolling_window_days}D"
        sigma = self.settings.sigma_threshold
        pieces = []
        for hub_label, sub in df.groupby("hub"):
            sub = sub.sort_values("interval_start").copy()
            s = sub.set_index("interval_start")["implied_hr"]
            min_periods = max(
                2,
                int(self.settings.rolling_window_days
                    * self.settings.min_periods_frac),
            )
            roll = s.rolling(window, min_periods=min_periods)
            sub["hr_mean"] = roll.mean().values
            sub["hr_std"] = roll.std().values
            with np.errstate(invalid="ignore", divide="ignore"):
                sub["hr_z"] = (sub["implied_hr"] - sub["hr_mean"]) / sub["hr_std"]
            sub["upper"] = sub["hr_mean"] + sigma * sub["hr_std"]
            sub["lower"] = sub["hr_mean"] - sigma * sub["hr_std"]
            sub["dislocation"] = sub["hr_z"].abs() > sigma
            pieces.append(sub)
        return pd.concat(pieces, ignore_index=True)

    # ------------------------------------------------------------------ #
    # spark spreads per unit class
    # ------------------------------------------------------------------ #
    def spark_spreads(self, market: str = "DAY_AHEAD_HOURLY", start=None) -> pd.DataFrame:
        """Long frame: interval_start, hub, unit_key, unit_label, heat_rate,
        spark_spread, in_the_money.
        """
        base = self.heat_rate_series(market=market, start=start)
        if base.empty:
            return pd.DataFrame(columns=[
                "interval_start", "hub", "unit_key", "unit_label",
                "heat_rate", "spark_spread", "in_the_money",
            ])
        rows = []
        for _, r in base.iterrows():
            power = r["lmp"]
            gas = r["gas_price"]
            if pd.isna(power) or pd.isna(gas):
                continue
            for u in UNIT_CLASSES:
                spark = power - (u.heat_rate * gas) - u.vom
                rows.append({
                    "interval_start": r["interval_start"],
                    "hub": r["hub"],
                    "unit_key": u.key,
                    "unit_label": u.label,
                    "heat_rate": u.heat_rate,
                    "spark_spread": spark,
                    "in_the_money": spark > 0,
                })
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ #
    # live snapshots for the UI strip / alerts
    # ------------------------------------------------------------------ #
    def latest_snapshot(self, market: str = "DAY_AHEAD_HOURLY") -> pd.DataFrame:
        """One row per hub at the most recent available interval, with the
        implied HR, its rolling band, dislocation flag, and per-unit in/out of
        money booleans.
        """
        hr = self.heat_rate_series(market=market)
        if hr.empty:
            return pd.DataFrame()
        latest = (hr.sort_values("interval_start")
                    .groupby("hub", as_index=False).tail(1))

        sparks = self.spark_spreads(market=market)
        result_rows = []
        for _, r in latest.iterrows():
            row = {
                "hub": r["hub"],
                "interval_start": r["interval_start"],
                "lmp": r["lmp"],
                "gas_price": r["gas_price"],
                "implied_hr": r["implied_hr"],
                "hr_mean": r["hr_mean"],
                "hr_std": r["hr_std"],
                "hr_z": r["hr_z"],
                "upper": r["upper"],
                "lower": r["lower"],
                "dislocation": bool(r["dislocation"]) if pd.notna(r["dislocation"]) else False,
            }
            if not sparks.empty:
                s = sparks[(sparks["hub"] == r["hub"]) &
                           (sparks["interval_start"] == r["interval_start"])]
                for _, sr in s.iterrows():
                    row[f"itm_{sr['unit_key']}"] = bool(sr["in_the_money"])
                    row[f"spark_{sr['unit_key']}"] = sr["spark_spread"]
            result_rows.append(row)
        return pd.DataFrame(result_rows)

    def active_alerts(self, market: str = "DAY_AHEAD_HOURLY", lookback_hours: int = 24) -> pd.DataFrame:
        """Recent intervals whose implied HR is a >sigma dislocation."""
        hr = self.heat_rate_series(market=market)
        if hr.empty:
            return pd.DataFrame(columns=[
                "interval_start", "hub", "implied_hr", "hr_mean",
                "hr_std", "hr_z", "direction",
            ])
        cutoff = hr["interval_start"].max() - pd.Timedelta(hours=lookback_hours)
        recent = hr[(hr["interval_start"] >= cutoff) & (hr["dislocation"] == True)].copy()  # noqa: E712
        if recent.empty:
            return pd.DataFrame(columns=[
                "interval_start", "hub", "implied_hr", "hr_mean",
                "hr_std", "hr_z", "direction",
            ])
        recent["direction"] = np.where(recent["hr_z"] > 0, "HIGH (rich)", "LOW (cheap)")
        return (recent[["interval_start", "hub", "implied_hr", "hr_mean",
                        "hr_std", "hr_z", "direction"]]
                .sort_values("interval_start", ascending=False)
                .reset_index(drop=True))

    # ------------------------------------------------------------------ #
    # net load / duck curve
    # ------------------------------------------------------------------ #
    def net_load_today(self) -> pd.DataFrame:
        """Net load = actual load - (solar + wind) for today, 15-min grain.

        Returns columns: interval_start, load, renewables, net_load.
        """
        pac_today = pd.Timestamp.now(tz="US/Central").normalize()
        start_utc = pac_today.tz_convert("UTC")

        load = self.storage.read_load(forecast=False, start=start_utc)
        fm = self.storage.read_fuel_mix(start=start_utc)
        if load.empty:
            return pd.DataFrame(columns=["interval_start", "load", "renewables", "net_load"])

        load = load[["interval_start", "load_mw"]].rename(columns={"load_mw": "load"})
        if not fm.empty:
            # ERCOT load lands on clean 5-min boundaries (:30:00) while fuel mix
            # is reported a few seconds off (:34:57), so an exact-timestamp merge
            # drops every renewable value. Align both to a 5-min grain first.
            load["_bucket"] = load["interval_start"].dt.round("5min")
            ren = fm[fm["fuel"].isin(["Solar", "Wind"])].copy()
            ren["_bucket"] = ren["interval_start"].dt.round("5min")
            ren = (ren.groupby("_bucket", as_index=False)["mw"].sum()
                      .rename(columns={"mw": "renewables"}))
            df = load.merge(ren, on="_bucket", how="left").drop(columns="_bucket")
        else:
            df = load.copy()
            df["renewables"] = np.nan

        df["renewables"] = df["renewables"].fillna(0)
        df["net_load"] = df["load"] - df["renewables"]
        return df.sort_values("interval_start").reset_index(drop=True)

    @staticmethod
    def evening_ramp(net_load: pd.DataFrame) -> dict:
        """Locate the evening net-load ramp (min after noon -> subsequent peak).

        Returns dict with ramp_start/ramp_end timestamps and MW magnitude, or an
        empty dict if it can't be determined.
        """
        if net_load.empty or net_load["net_load"].dropna().empty:
            return {}
        nl = net_load.dropna(subset=["net_load"]).copy()
        nl["local"] = nl["interval_start"].dt.tz_convert("US/Central")
        afternoon = nl[nl["local"].dt.hour >= 12]
        if afternoon.empty:
            return {}
        trough_idx = afternoon["net_load"].idxmin()
        trough_time = nl.loc[trough_idx, "interval_start"]
        after = nl[nl["interval_start"] >= trough_time]
        peak_idx = after["net_load"].idxmax()
        return {
            "ramp_start": trough_time,
            "ramp_end": nl.loc[peak_idx, "interval_start"],
            "ramp_mw": float(nl.loc[peak_idx, "net_load"] - nl.loc[trough_idx, "net_load"]),
            "trough_mw": float(nl.loc[trough_idx, "net_load"]),
            "peak_mw": float(nl.loc[peak_idx, "net_load"]),
        }
