"""Central configuration for Sparkedge ERCOT.

Everything that a user might reasonably want to tune lives here: which ERCOT
trading hubs to monitor, the representative generating-unit heat rates used for
spark-spread economics, gas-price source wiring, rolling-window statistics, and
API/rate-limit behaviour.

Values can be overridden with environment variables (see ``Settings``) so the
package works both as a checked-in config file and in a 12-factor deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Trading hubs
# --------------------------------------------------------------------------- #
# ERCOT exposes its trading hubs as settlement-point aggregates. ``node`` is the
# exact identifier gridstatus returns in the "Location" column of get_spp when
# ``location_type="Trading Hub"``; ``label`` is the friendly name used in the UI.
# ERCOT has no per-hub gas feed, so every hub maps to the same reference gas
# (EIA Henry Hub); ``gas_region`` is retained only as a bookkeeping label.
@dataclass(frozen=True)
class Hub:
    label: str          # e.g. "Houston"
    node: str           # e.g. "HB_HOUSTON"
    gas_region: str     # gas proxy label (ERCOT: always Henry Hub)
    description: str = ""


HUBS: list[Hub] = [
    Hub(
        label="Houston",
        node="HB_HOUSTON",
        gas_region="HENRY_HUB",
        description="ERCOT Houston trading hub (HB_HOUSTON)",
    ),
    Hub(
        label="North",
        node="HB_NORTH",
        gas_region="HENRY_HUB",
        description="ERCOT North trading hub (HB_NORTH)",
    ),
    Hub(
        label="West",
        node="HB_WEST",
        gas_region="HENRY_HUB",
        description="ERCOT West trading hub (HB_WEST)",
    ),
    Hub(
        label="South",
        node="HB_SOUTH",
        gas_region="HENRY_HUB",
        description="ERCOT South trading hub (HB_SOUTH)",
    ),
]

HUBS_BY_LABEL: dict[str, Hub] = {h.label: h for h in HUBS}
HUBS_BY_NODE: dict[str, Hub] = {h.node: h for h in HUBS}


# --------------------------------------------------------------------------- #
# Representative generating-unit classes
# --------------------------------------------------------------------------- #
# Heat rate is expressed in MMBtu/MWh. Spark spread for a given unit is:
#     spark = power_price - (unit_heat_rate * gas_price) - vom
# A unit is "in the money" when its spark spread is positive.
@dataclass(frozen=True)
class UnitClass:
    key: str            # short id used in the db / config
    label: str          # friendly name
    heat_rate: float    # MMBtu/MWh
    vom: float = 0.0     # variable O&M adder in $/MWh (optional)
    color: str = "#888888"


UNIT_CLASSES: list[UnitClass] = [
    UnitClass(
        key="cc_68",
        label="6.8 HR Combined-Cycle",
        heat_rate=6.8,
        vom=2.0,
        color="#2ca02c",
    ),
    UnitClass(
        key="cc_75",
        label="7.5 HR Combined-Cycle",
        heat_rate=7.5,
        vom=2.5,
        color="#1f77b4",
    ),
    UnitClass(
        key="peaker_105",
        label="10.5 HR Peaker",
        heat_rate=10.5,
        vom=4.5,
        color="#d62728",
    ),
]

UNIT_CLASSES_BY_KEY: dict[str, UnitClass] = {u.key: u for u in UNIT_CLASSES}


# --------------------------------------------------------------------------- #
# Runtime settings (env-overridable)
# --------------------------------------------------------------------------- #
def _default_db_path() -> str:
    # Store the cache next to the package by default so it survives restarts.
    root = Path(os.environ.get("SPARKEDGE_HOME", Path.home() / ".sparkedge_ercot"))
    root.mkdir(parents=True, exist_ok=True)
    return str(root / "sparkedge_ercot.db")


@dataclass
class Settings:
    # --- storage ---
    db_path: str = field(default_factory=_default_db_path)

    # --- rolling statistics ---
    rolling_window_days: int = 30      # window for HR mean / std
    sigma_threshold: float = 2.0       # dislocation flag threshold (in std devs)
    min_periods_frac: float = 0.5      # min fraction of window needed for a stat

    # --- API rate limiting ---
    # gridstatus already sleeps between paginated calls; we add our own inter-call
    # sleep on top. ERCOT's public API is friendlier than CAISO OASIS, but we keep
    # the knobs so behaviour is tunable.
    oasis_sleep_seconds: int = 3       # passed to gridstatus get_spp
    inter_call_sleep_seconds: float = 2.0  # our own sleep between distinct pulls
    max_retries: int = 3
    retry_backoff_seconds: float = 10.0

    # --- SSL handling ---
    # Some networks (corporate proxies, HTTPS-inspecting antivirus, certain VPNs)
    # break certificate validation for the public data endpoints gridstatus uses.
    # Symptom:
    #   SSL: CERTIFICATE_VERIFY_FAILED ... self-signed certificate in chain.
    # When True, we relax TLS verification so load / fuel-mix (and the duck curve)
    # work on those networks. Off by default; set env SPARKEDGE_INSECURE_SSL=1 to
    # enable.
    insecure_ssl_caiso: bool = field(
        default_factory=lambda: os.environ.get("SPARKEDGE_INSECURE_SSL", "")
        .strip().lower() in {"1", "true", "yes", "on"}
    )

    # --- EIA (Henry Hub) ---
    # Henry Hub daily spot: route natural-gas/pri/fut, series RNGWHHD.
    eia_api_key: str | None = field(
        default_factory=lambda: os.environ.get("EIA_API_KEY")
    )
    eia_henry_hub_series: str = "RNGWHHD"
    eia_route: str = "natural-gas/pri/fut"

    # --- gas price source preference ---
    # ERCOT has no per-hub gas feed, so Henry Hub (EIA) is the reference gas for
    # every hub. "eia" -> use Henry Hub as the primary reference gas.
    gas_source_primary: str = "eia"

    # --- backfill defaults ---
    backfill_days: int = 45            # enough to seed a 30d rolling window

    def __post_init__(self) -> None:
        env = os.environ
        if "SPARKEDGE_DB_PATH" in env:
            self.db_path = env["SPARKEDGE_DB_PATH"]
        if "SPARKEDGE_ROLLING_DAYS" in env:
            self.rolling_window_days = int(env["SPARKEDGE_ROLLING_DAYS"])
        if "SPARKEDGE_SIGMA" in env:
            self.sigma_threshold = float(env["SPARKEDGE_SIGMA"])
        if "SPARKEDGE_GAS_SOURCE" in env:
            self.gas_source_primary = env["SPARKEDGE_GAS_SOURCE"].lower()
        if "SPARKEDGE_OASIS_SLEEP" in env:
            self.oasis_sleep_seconds = int(env["SPARKEDGE_OASIS_SLEEP"])


# A module-level singleton is convenient for the app; callers may also build
# their own Settings() for tests.
SETTINGS = Settings()
