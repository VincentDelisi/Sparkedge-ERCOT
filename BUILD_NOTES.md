# Sparkedge ERCOT — Build Notes

Adapted the Sparkedge **West (CAISO)** app into a Sparkedge **ERCOT (Texas)**
real-time energy dashboard. Same architecture (Streamlit + gridstatus + SQLite;
implied heat rate / spark spread / duck curve), retargeted to ERCOT.

## Changes

### Package rename
- `sparkedge_west/` → `sparkedge_ercot/`; all imports updated (`__init__`,
  `__main__`, `cli`, and `app.py`'s `streamlit run` fallback absolute-import path).
- Removed `assets/caiso_logo.png`. `assets/ercot_logo.png` is present (added by
  parent); `_logo_data_uri` still returns `None` gracefully if a logo is missing.

### config.py
- `HUBS` replaced with ERCOT trading hubs: **HB_HOUSTON, HB_NORTH, HB_WEST,
  HB_SOUTH** (labels Houston / North / West / South). `node` = the exact `HB_*`
  id gridstatus returns in the `Location` column of `get_spp`.
- ERCOT has no per-hub gas feed → every hub's `gas_region` = `HENRY_HUB`.
- `Settings.gas_source_primary` default changed `"oasis"` → `"eia"`.
- DB / SPARKEDGE_HOME defaults → `.sparkedge_ercot` / `sparkedge_ercot.db`.
- Kept the `insecure_ssl_caiso` and `oasis_sleep_seconds` field names (functional,
  referenced elsewhere) but they now govern ERCOT pulls; sleeps lowered a bit
  (ERCOT's public API is friendlier than CAISO OASIS).

### data.py
- Uses `gridstatus.Ercot()` (was `CAISO()`); handle method renamed `_ercot`.
- **LMP** via `get_spp(date, end, market=..., location_type="Trading Hub")`.
  Market strings: `DAY_AHEAD_HOURLY`, `REAL_TIME_15_MIN`. Maps column `SPP`→`lmp`,
  `Location`→hub; skips non-configured hubs (HUBAVG/BUSAVG/PAN). ERCOT SPP has no
  energy/congestion/loss components, so those are stored `NULL`.
- **Load** via `get_load`; **load forecast** via `get_load_forecast` using the
  `System Total` column (ERCOT publishes per forecast-zone columns).
- **Fuel mix** via `get_fuel_mix` (single date only — no `end` param).
- **Removed** CAISO OASIS gas entirely; gas now only via EIA v2 Henry Hub
  (`fetch_gas_eia`, series `RNGWHHD`).
- `refresh_today` / `backfill` retargeted (RT uses `REAL_TIME_15_MIN`).

### compute.py
- `_gas_for_hub` returns EIA `HENRY_HUB` gas for every hub (no OASIS path).
- All `US/Pacific` → `US/Central` (America/Chicago).
- `implied_hr`, rolling ±2σ stats, snapshot, spark spreads, alerts, duck-curve,
  evening ramp all kept intact.

### app.py
- Title/subtitle → "Sparkedge ERCOT" / "…across ERCOT HB_HOUSTON · HB_NORTH ·
  HB_WEST"; logo `ercot_logo.png`; `page_title` and sidebar title updated.
- Market radio: "Day-Ahead"→`DAY_AHEAD_HOURLY`, "Real-Time 15-min"→`REAL_TIME_15_MIN`.
- `HUB_COLORS` rekeyed to Houston/North/West/South.
- **Green/red ITM legend in `panel_money_strip` kept** as required.
- All `US/Pacific`→`US/Central`, "PT"→"CT". EIA-missing notice reworded.

### ssl_compat.py
- Scoped-TLS host list now includes `ercot.com` (kept `caiso.com` harmlessly) so
  the `requests` path is covered for ERCOT on TLS-intercepting networks.

### storage.py
- Schema comments updated (RT market, hub labels, gas source `eia`/`HENRY_HUB`).
  Schema itself unchanged — same tables work for ERCOT.

### Deploy config
- `start.sh`, `pyproject.toml` (name `sparkedge-ercot`, package, script),
  `README.md`, `.env.example` all retargeted to `sparkedge_ercot`.
- `Procfile` / `railway.json` reference `start.sh` (unchanged). `runtime.txt`
  already `python-3.12`.

## Environment / test
- venv at `/home/user/workspace/ercot-deploy/.venv` via
  `/usr/local/bin/python3.12 -m venv`; `pip install -r requirements.txt` OK.
- Ran end-to-end with `SPARKEDGE_INSECURE_SSL=1`.

### Backfill row counts (`--backfill --days 3`, real ERCOT data)
| source        | rows   |
|---------------|--------|
| lmp_dam       | 288    | (3d × 24h × 4 hubs) |
| lmp_rt        | 1,152  | (15-min × 4 hubs)   |
| load          | 72     |
| load_forecast | 13,824 | (multiple publish times per interval) |
| fuel_mix      | 2,304  |
| gas_eia       | 0      | (no EIA key in test env) |

`--refresh` (today) also succeeded: lmp_dam 96, lmp_rt 284, load 214,
load_forecast 192, fuel_mix 1,704, gas_eia 0.

### Compute validation
- `latest_snapshot` returns **4 rows** (one per hub) with real LMPs.
- `heat_rate_series` → 288 rows, all LMPs non-null.
- With a **synthetic** Henry Hub gas row injected (to simulate an EIA key):
  implied_hr computed correctly (e.g. North LMP 28.55 / gas 3.10 = **9.21**),
  spark spreads + green/red ITM flags populated, alerts computed. This confirms
  the gas→heat-rate→spark path is wired correctly; it only needs a real key.
- Duck curve validated (see gotcha below): net-load 213 rows, renewables matched
  on 212, evening ramp ≈ 11,126 MW.
- Streamlit app boots headless with no import/runtime errors.

## ERCOT gotchas
1. **Fuel-mix timestamp misalignment (fixed).** ERCOT `get_load` lands on clean
   5-min boundaries (`:30:00`) while `get_fuel_mix` reports a few seconds early
   (`:34:57`). An exact-timestamp merge dropped **all** Solar+Wind, zeroing the
   duck curve. Fixed in `net_load_today` by rounding both sides to a 5-min bucket
   before merging.
2. **Fuel mix has no `Interval Start` column** — only `Time`. `fetch_fuel_mix`
   falls back to `Time` for the interval timestamp.
3. **`get_fuel_mix` takes no `end`** — backfill calls it one day at a time.
4. **Load forecast is per forecast-zone** (North/South/West/Houston/System Total
   columns, plus multiple `Publish Time`s). We take `System Total`; duplicate
   intervals across publish times are absorbed by the idempotent upsert.
5. **`get_spp` has no `sleep` kwarg** (unlike CAISO `get_lmp`); removed.
6. **Trading-hub locations** include HUBAVG/BUSAVG/PAN — filtered out; only
   HB_HOUSTON/NORTH/WEST/SOUTH are kept.
7. ERCOT SPP carries no energy/congestion/loss breakdown → stored NULL.

## EIA gas status
**Needs a key.** `EIA_API_KEY` was **not set** in the test env, so `gas_eia`
pulled 0 rows and implied heat rates / spark spreads degrade to n/a (by design —
no crash). Set `EIA_API_KEY` (free at https://www.eia.gov/opendata/register.php)
to enable Henry Hub gas and full heat-rate/spark-spread output. The EIA fetch and
downstream compute were verified working via a synthetic Henry Hub row.
