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

from .config import HUBS_BY_LABEL, SETTINGS, UNIT_CLASSES, Hub, Settings
from .storage import Storage

log = logging.getLogger(__name__)


class Analytics:
    def __init__(self, storage: Storage, settings: Settings = SETTINGS):
        self.storage = storage
        self.settings = settings

    # ------------------------------------------------------------------ #
    # gas price series -- one representative $/MMBtu series per hub
    # ------------------------------------------------------------------ #
    def _gas_series(self, source: str, region: str, start=None) -> pd.DataFrame:
        """Read a daily gas price series (columns: date, gas_price) from the
        cache for a given (source, region), collapsing any duplicate-day rows
        with a mean. Returns an empty typed frame if nothing is cached.
        """
        g = self.storage.read_gas(source=source, region=region, start=start)
        if g.empty:
            return pd.DataFrame(columns=["date", "gas_price"])
        g = g.dropna(subset=["price"]).copy()
        if g.empty:
            return pd.DataFrame(columns=["date", "gas_price"])
        g["date"] = g["interval_start"].dt.tz_convert("US/Central").dt.normalize()
        return (g.groupby("date", as_index=False)["price"]
                 .mean().rename(columns={"price": "gas_price"}))

    def _gas_for_hub(self, hub_label: str, start=None) -> pd.DataFrame:
        """Return a daily gas price series for a hub.

        Columns: date, gas_price, gas_estimated (bool).

        Most ERCOT hubs have no native gas feed, so they use the EIA Henry Hub
        daily spot price as a reference gas (``gas_estimated`` is always False
        for these -- it is a deliberate proxy choice, not a data gap).

        HB_WEST is different: it is wired to real Waha (Permian) gas via
        OilPriceAPI (see config.HUBS / data.fetch_gas_waha). OilPriceAPI's free
        tier reliably gives only the *latest* daily quote, so Waha history can
        be sparse or missing entirely. To keep West's rolling heat-rate window
        populated we apply a graceful fallback:

          1. Read whatever real Waha rows are cached.
          2. Read Henry Hub daily rows (deep history, always available once an
             EIA key is set).
          3. Compute the observed Waha-minus-HenryHub basis on the dates where
             both series overlap (median basis over the overlap).
          4. For West dates with a real Waha price, use it as-is
             (gas_estimated=False). For older West dates with no Waha price,
             estimate gas = HenryHub_that_day + observed_basis
             (gas_estimated=True) so the basis, which is typically negative,
             discounts Henry Hub the way Waha actually trades.

        If there is no Waha data at all (e.g. no OILPRICEAPI_KEY), West falls
        back entirely to Henry Hub, all rows flagged gas_estimated=True, so the
        hub still shows *something* rather than going blank -- callers/UI
        should label this clearly as an HH proxy.
        """
        hub = HUBS_BY_LABEL.get(hub_label)
        empty = pd.DataFrame(columns=["date", "gas_price", "gas_estimated"])
        if hub is None:
            return empty

        henry = self._gas_series("eia", "HENRY_HUB", start=start)

        if hub.gas_source != "oilpriceapi":
            # Standard proxy hubs: Henry Hub as-is, not an estimate.
            if henry.empty:
                return empty
            out = henry.copy()
            out["gas_estimated"] = False
            return out

        # --- HB_WEST: real Waha with graceful basis-fallback to Henry Hub ---
        waha = self._gas_series("oilpriceapi", hub.gas_region, start=start)

        if waha.empty:
            # No Waha data at all -- fall back entirely to Henry Hub, flagged.
            if henry.empty:
                return empty
            out = henry.copy()
            out["gas_estimated"] = True
            return out

        if henry.empty:
            # Have Waha but no Henry Hub to backfill older dates with -- just
            # use the real Waha rows we have.
            out = waha.copy()
            out["gas_estimated"] = False
            return out

        merged = pd.merge(
            waha, henry, on="date", how="inner", suffixes=("_waha", "_hh"),
        )
        if not merged.empty:
            basis = float((merged["gas_price_waha"] - merged["gas_price_hh"]).median())
        else:
            basis = float("nan")

        waha_dates = set(waha["date"])
        hh_only = henry[~henry["date"].isin(waha_dates)].copy()
        if not hh_only.empty and pd.notna(basis):
            hh_only["gas_price"] = hh_only["gas_price"] + basis
            hh_only["gas_estimated"] = True
        elif not hh_only.empty:
            # No overlap to compute a basis from yet (e.g. Waha history is
            # only 1 day old) -- still surface Henry Hub as an estimate rather
            # than leaving a hole in the window.
            hh_only["gas_estimated"] = True
        else:
            hh_only["gas_estimated"] = pd.Series(dtype=bool)

        waha_out = waha.copy()
        waha_out["gas_estimated"] = False

        out = pd.concat([waha_out, hh_only[["date", "gas_price", "gas_estimated"]]],
                         ignore_index=True)
        return out.sort_values("date").reset_index(drop=True)

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
                "gas_estimated",
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
                sub["gas_estimated"] = False
            else:
                # EIA Henry Hub / Waha daily spot prices lag a few business
                # days, so an exact same-day join leaves today's LMPs without
                # gas. Use an as-of (backward) join so each interval picks up
                # the most recent available gas price on or before its date,
                # then forward-fill any remaining leading gaps.
                gas = gas.sort_values("date").reset_index(drop=True)
                sub = sub.sort_values("date").reset_index(drop=True)
                sub = pd.merge_asof(
                    sub, gas, on="date", direction="backward",
                )
                # if the very first days precede all gas history, backfill from
                # the earliest known gas price so heat rates still compute.
                sub["gas_price"] = sub["gas_price"].ffill().bfill()
                sub["gas_estimated"] = sub["gas_estimated"].ffill().bfill()
                sub["gas_estimated"] = sub["gas_estimated"].fillna(False).astype(bool)
            # Guard the divide: a near-zero (or negative-near-zero, which Waha
            # can genuinely be) gas price makes lmp/gas explode toward +-inf
            # rather than a meaningful heat rate, so treat |gas| < 0.10 as
            # undefined instead of computing a spurious huge/inf number.
            safe_gas = sub["gas_price"].where(sub["gas_price"].abs() >= 0.10)
            sub["implied_hr"] = sub["lmp"] / safe_gas
            frames.append(sub)

        if not frames:
            return pd.DataFrame()

        out = pd.concat(frames, ignore_index=True)
        out = out.sort_values(["hub", "interval_start"]).reset_index(drop=True)
        out = self._add_rolling_stats(out)
        cols = ["interval_start", "hub", "lmp", "gas_price", "implied_hr",
                "hr_mean", "hr_std", "hr_z", "dislocation", "upper", "lower",
                "gas_estimated"]
        return out[cols]

    def _add_rolling_stats(self, df: pd.DataFrame) -> pd.DataFrame:
        """Attach a normalized dislocation signal to each interval.

        Two corrections over a naive pooled mean +/- 2 sigma band:

        1. **Log heat rate.** Implied HR is bounded near zero and has a fat
           right tail from scarcity pricing, so it is far from normal. We take
           z-scores of ln(HR); a symmetric band on the log scale is much closer
           to a true ~5% tail and stops firing constantly on the high side
           while never firing on the low side.
        2. **Hour-of-day conditioning.** HR has a strong diurnal shape (low
           overnight, high on the evening ramp). Pooling all hours means the
           ramp trips the alert every single day -- that is the diurnal
           pattern, not a dislocation. We compute mu/sigma from the trailing
           window of the *same hour of day* so a breach means "rich/cheap
           relative to how this hour normally behaves."

        Bands (hr_mean/upper/lower) are reported back on the natural HR scale
        (via exp) so the chart still reads in heat-rate units.
        """
        window_days = self.settings.rolling_window_days
        sigma = self.settings.sigma_threshold
        # hour-conditioned stats need more calendar history than a pooled
        # window, since each hour-of-day bucket only sees ~1 obs/day. Widen the
        # lookback to >=60d (or the configured window).
        cond_days = max(window_days, 60)
        # IMPORTANT: min_periods here is a count of *same-hour* observations,
        # NOT a fraction of the calendar window. Reusing min_periods_frac * days
        # (=30) demanded ~30 days of history at each specific hour before ANY
        # band/z-score appeared -- with a 45d backfill that left the ±2σ band
        # blank and, worse, hr_z NaN everywhere so the alert could never fire
        # ("no dislocations" was a dead detector, not a calm market). A stable
        # per-hour mean/std needs only ~10 samples, so floor at that.
        min_periods = self.settings.hod_min_samples

        pieces = []
        for hub_label, sub in df.groupby("hub"):
            sub = sub.sort_values("interval_start").reset_index(drop=True).copy()
            local = sub["interval_start"].dt.tz_convert("US/Central")
            sub["_hod"] = local.dt.hour
            with np.errstate(invalid="ignore", divide="ignore"):
                sub["_log_hr"] = np.log(sub["implied_hr"].where(sub["implied_hr"] > 0))

            hod_mean = np.full(len(sub), np.nan)
            hod_std = np.full(len(sub), np.nan)
            # rolling stats within each hour-of-day bucket, over a trailing
            # calendar window, computed on log(HR).
            for _hod, g in sub.groupby("_hod"):
                g = g.sort_values("interval_start")
                s = g.set_index("interval_start")["_log_hr"]
                roll = s.rolling(f"{cond_days}D", min_periods=min_periods)
                m = roll.mean()
                sd = roll.std()
                hod_mean[g.index] = m.values
                hod_std[g.index] = sd.values

            sub["_logmean"] = hod_mean
            sub["_logstd"] = hod_std
            with np.errstate(invalid="ignore", divide="ignore"):
                sub["hr_z"] = (sub["_log_hr"] - sub["_logmean"]) / sub["_logstd"]
            # report bands back on the natural HR scale for the chart
            sub["hr_mean"] = np.exp(sub["_logmean"])
            sub["upper"] = np.exp(sub["_logmean"] + sigma * sub["_logstd"])
            sub["lower"] = np.exp(sub["_logmean"] - sigma * sub["_logstd"])
            # keep an hr_std column for display continuity (natural-scale approx)
            sub["hr_std"] = (sub["upper"] - sub["lower"]) / (2 * sigma)
            sub["dislocation"] = sub["hr_z"].abs() > sigma
            sub = sub.drop(columns=["_hod", "_log_hr", "_logmean", "_logstd"])
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
            hub_cfg = HUBS_BY_LABEL.get(r["hub"])
            gas_estimated = bool(r.get("gas_estimated")) if pd.notna(r.get("gas_estimated")) else False
            row = {
                "hub": r["hub"],
                "interval_start": r["interval_start"],
                "lmp": r["lmp"],
                "gas_price": r["gas_price"],
                "gas_label": hub_cfg.gas_label if hub_cfg is not None else "",
                "gas_estimated": gas_estimated,
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

    _ALERT_COLS = [
        "interval_start", "direction", "hubs", "n_hubs",
        "max_abs_z", "detail",
    ]

    def active_alerts(self, market: str = "DAY_AHEAD_HOURLY", lookback_hours: int = 24) -> pd.DataFrame:
        """Recent >sigma HR dislocations, **deduped to system-level events**.

        Multiple hubs breaching the same direction in the same interval is ONE
        event (usually a shared marginal-unit / scarcity condition), not N
        separate alerts. We collapse by (interval, direction) and list the
        affected hubs, ranked by the strongest |z| in the group.
        """
        hr = self.heat_rate_series(market=market)
        if hr.empty:
            return pd.DataFrame(columns=self._ALERT_COLS)
        cutoff = hr["interval_start"].max() - pd.Timedelta(hours=lookback_hours)
        recent = hr[(hr["interval_start"] >= cutoff) & (hr["dislocation"] == True)].copy()  # noqa: E712
        if recent.empty:
            return pd.DataFrame(columns=self._ALERT_COLS)
        recent["direction"] = np.where(recent["hr_z"] > 0, "HIGH (rich)", "LOW (cheap)")

        events = []
        for (ts, direction), g in recent.groupby(["interval_start", "direction"]):
            g = g.reindex(g["hr_z"].abs().sort_values(ascending=False).index)
            hubs = list(g["hub"])
            detail = ", ".join(
                f"{h} HR {ihr:.1f} (z {z:+.1f})"
                for h, ihr, z in zip(g["hub"], g["implied_hr"], g["hr_z"])
            )
            events.append({
                "interval_start": ts,
                "direction": direction,
                "hubs": ", ".join(hubs),
                "n_hubs": len(hubs),
                "max_abs_z": float(g["hr_z"].abs().max()),
                "detail": detail,
            })
        return (pd.DataFrame(events, columns=self._ALERT_COLS)
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

        df = df.sort_values("interval_start").reset_index(drop=True)
        # Do NOT blanket-fill missing renewables with 0: the two feeds can be
        # published one interval out of sync, so the newest load interval often
        # has no fuel-mix match yet. Filling 0 there makes net_load jump up to
        # equal load for that single point (the spurious vertical drop the
        # review flagged). Instead:
        #   * interior gaps -> interpolate (renewables move smoothly),
        #   * unmatched leading/trailing edge intervals -> drop, so load and
        #     net_load always start and end on the same interval.
        if df["renewables"].notna().any():
            first = df["renewables"].first_valid_index()
            last = df["renewables"].last_valid_index()
            df = df.loc[first:last].copy()
            df["renewables"] = df["renewables"].interpolate(limit_direction="both")
        else:
            df["renewables"] = 0.0
        df["net_load"] = df["load"] - df["renewables"]
        return df.reset_index(drop=True)

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
