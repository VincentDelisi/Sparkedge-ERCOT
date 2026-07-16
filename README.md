# ⚡ Sparkedge ERCOT

Implied **heat rate** and **spark spread** monitor for the ERCOT (Texas) power market.

Sparkedge ERCOT pulls ERCOT prices, load, and fuel mix plus natural-gas prices,
caches everything to SQLite, computes implied market heat rates and spark
spreads for three representative generating-unit classes, flags statistical
dislocations, and renders it all in a Streamlit dashboard.

---

## What it does

**Data layer** (`gridstatus` + EIA)
- ERCOT **day-ahead hourly** and **15-minute real-time** SPP for the **HB_HOUSTON**, **HB_NORTH**, **HB_WEST** (and **HB_SOUTH**) trading hubs.
- ERCOT **load**, **load forecast**, and **fuel mix**.
- Natural gas from **EIA Henry Hub daily spot** (`RNGWHHD`). ERCOT has no per-hub
  gas feed, so Henry Hub is the reference gas for every hub.
- Everything cached to **SQLite** (idempotent upserts) so APIs aren't re-hit.
- Sleeps between calls + retry/backoff so a flaky pull never aborts a run.

**Compute layer**
- **Implied market heat rate** = power price ÷ gas price (MMBtu/MWh).
- **Spark spread** = power price − (unit heat rate × gas price) − VOM, for:
  - **6.8 HR** combined-cycle, **7.5 HR** combined-cycle, **10.5 HR** peaker.
- **Rolling 30-day mean & std** of the implied HR per hub.
- **Flags** any interval where the current implied HR is more than **±2σ** from the mean.

**Display layer** (Streamlit)
1. Implied-heat-rate time-series per hub with the **±2σ band** overlaid.
2. **Live strip** — which unit classes are currently in the money at each hub.
3. **Duck-curve** net-load chart for today with the **evening ramp highlighted**.
4. **Alerts panel** listing current heat-rate dislocations.

Every panel **degrades instead of crashing** — a down source just shows a notice.

---

## Install

Requires Python 3.10–3.13.

```bash
cd ercot-deploy
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# or install as a package (gives the `sparkedge-ercot` console script):
pip install -e .
```

### EIA key (required for heat rates)

Henry Hub gas needs a free EIA API key. Without it, gas is unavailable and
implied heat rates / spark spreads show as n/a (prices, load, fuel mix, and the
duck curve still work).

```bash
cp .env.example .env      # then edit and set EIA_API_KEY=...
export EIA_API_KEY=your_key_here
```

---

## Usage

```bash
# 1) Seed the cache (~45 days -> enough to fill the 30-day rolling window)
python -m sparkedge_ercot --backfill
python -m sparkedge_ercot --backfill --days 60     # custom horizon

# 2) Live refresh of today's data
python -m sparkedge_ercot --refresh

# 3) Launch the dashboard
python -m sparkedge_ercot --serve
#   (equivalently: streamlit run sparkedge_ercot/app.py)

# Diagnostics
python -m sparkedge_ercot --status
```

The dashboard also has a **"Refresh today's data"** button in the sidebar.

---

## Configuration

All tunables live in [`sparkedge_ercot/config.py`](sparkedge_ercot/config.py):

- **`HUBS`** — trading-hub labels and ERCOT settlement-point ids (`HB_*`).
- **`UNIT_CLASSES`** — the three representative unit heat rates (and optional VOM).
- **`Settings`** — rolling window (30d), σ threshold (2.0), inter-call sleeps,
  retries, gas-source preference (`eia`), backfill horizon. Many are
  env-overridable (see `.env.example`).

---

## Package layout

```
ercot-deploy/
├── pyproject.toml
├── requirements.txt
├── .env.example
├── README.md
└── sparkedge_ercot/
    ├── __init__.py
    ├── __main__.py       # enables `python -m sparkedge_ercot`
    ├── config.py         # hubs, unit heat rates, settings
    ├── storage.py        # SQLite cache (idempotent upserts, safe reads)
    ├── data.py           # gridstatus + EIA fetchers (retry, rate-limit, graceful)
    ├── compute.py        # heat rates, spark spreads, rolling stats, alerts, net load
    ├── cli.py            # --backfill / --refresh / --status / --serve
    └── app.py            # Streamlit dashboard (4 panels)
```

---

## Notes & assumptions

- **Gas:** ERCOT has no per-hub gas feed, so **EIA Henry Hub** daily spot is the
  reference gas for every hub. An `EIA_API_KEY` is required for heat rates.
- **Real-time backfill** is limited to the most recent 7 days by default (15-min
  RT data is large); day-ahead + gas backfill the full horizon.
- **Heat rate** uses same-day gas (daily grain) joined to each price interval.
- Spark spreads include a small VOM adder per unit class (editable in config);
  set VOM to 0 for a pure clean-spark comparison.
- All local times are **US/Central** (America/Chicago).

Data: ERCOT via [gridstatus](https://github.com/gridstatus/gridstatus);
natural gas via the [EIA Open Data API](https://www.eia.gov/opendata/).

---

## Troubleshooting

**`SSL: CERTIFICATE_VERIFY_FAILED` on load / fuel-mix (empty duck curve).**
Some networks (corporate proxies, VPNs, HTTPS-inspecting antivirus) re-sign the
TLS chain for ERCOT's public endpoints with a certificate Python doesn't trust.
Enable the scoped TLS relaxation and re-run:

```bash
# macOS / Linux / Git Bash
export SPARKEDGE_INSECURE_SSL=1

# Windows PowerShell
$env:SPARKEDGE_INSECURE_SSL = "1"
```

Then `python -m sparkedge_ercot --backfill` (or --refresh). This disables TLS
verification **for ERCOT hosts only**; every other HTTPS request stays verified.
The proper alternative is to install your proxy's root CA into the system trust
store.

**Windows: `python` opens the Microsoft Store / `venv` won't create.**
Windows' App-execution-alias stub is shadowing real Python. Use `py` instead
(`py -m venv .venv`), or turn off the aliases in Settings -> Apps -> Advanced
app settings -> App execution aliases (`python.exe`, `python3.exe`).

**Windows PowerShell: `Activate.ps1` blocked ("running scripts is disabled").**
Skip activation and call the venv Python directly:
`.venv\Scripts\python.exe -m sparkedge_ercot --serve`.
