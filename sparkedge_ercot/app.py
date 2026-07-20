"""Streamlit dashboard for Sparkedge ERCOT.

Panels
------
1. Implied heat rate time-series per hub with the +/-2 sigma rolling band.
2. Live "in the money" strip: which of the three unit classes are ITM at each hub.
3. Net-load duck curve for today with the evening ramp highlighted.
4. Alerts panel: current heat-rate dislocations (|z| > sigma threshold).

Everything reads from the SQLite cache. If the cache is empty or a source is
missing, the relevant panel shows an informational message instead of crashing.
Use the sidebar "Refresh today's data" button (or the CLI --refresh/--backfill)
to populate the cache.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Streamlit runs this file as a top-level script (`streamlit run app.py`), which
# breaks package-relative imports. Support both invocation styles: relative when
# imported as part of the package, absolute (with the parent dir on sys.path)
# when executed directly by Streamlit.
try:
    from .config import HUBS, UNIT_CLASSES, SETTINGS
    from .storage import Storage
    from .compute import Analytics
    from .data import DataService
except ImportError:  # pragma: no cover - direct `streamlit run` path
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sparkedge_ercot.config import HUBS, UNIT_CLASSES, SETTINGS
    from sparkedge_ercot.storage import Storage
    from sparkedge_ercot.compute import Analytics
    from sparkedge_ercot.data import DataService

logging.getLogger("gridstatus").setLevel(logging.WARNING)

st.set_page_config(page_title="Sparkedge ERCOT", layout="wide",
                   page_icon="⚡", menu_items={})

# Hide Streamlit's default chrome (main menu, "Deploy" button, header, footer)
# so the app looks native when embedded as a tab inside the Sparkedge site.
st.markdown(
    """
    <style>
      #MainMenu {visibility: hidden;}
      header {visibility: hidden;}
      footer {visibility: hidden;}
      [data-testid="stToolbar"] {display: none !important;}
      [data-testid="stDecoration"] {display: none !important;}
      [data-testid="stStatusWidget"] {display: none !important;}
      .stAppDeployButton {display: none !important;}
      .block-container {padding-top: 2rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

HUB_COLORS = {"Houston": "#e45756", "North": "#4c78a8",
              "West": "#f58518", "South": "#54a24b"}


# --------------------------------------------------------------------------- #
# resources (cached across reruns)
# --------------------------------------------------------------------------- #
@st.cache_resource
def get_storage() -> Storage:
    return Storage(SETTINGS.db_path)


def get_analytics() -> Analytics:
    return Analytics(get_storage(), SETTINGS)


# --------------------------------------------------------------------------- #
# sidebar controls
# --------------------------------------------------------------------------- #
def sidebar() -> dict:
    st.sidebar.title("⚡ Sparkedge ERCOT")
    st.sidebar.caption("Implied heat rates & spark spreads · ERCOT")

    market_label = st.sidebar.radio(
        "Market",
        ["Day-Ahead", "Real-Time 15-min"],
        index=0,
        help="Which ERCOT SPP market to analyze.",
    )
    market = "DAY_AHEAD_HOURLY" if market_label == "Day-Ahead" else "REAL_TIME_15_MIN"

    st.sidebar.divider()
    st.sidebar.subheader("Data")
    storage = get_storage()
    cov = storage.coverage()
    st.sidebar.caption(
        f"LMP: {cov.get('lmp', 0):,} · Gas: {cov.get('gas_price', 0):,} · "
        f"Load: {cov.get('load_actual', 0):,} · Fuel mix: {cov.get('fuel_mix', 0):,}"
    )
    last = storage.get_meta("last_refresh")
    if last:
        st.sidebar.caption(f"Last refresh: {last[:19]} UTC")

    if st.sidebar.button("🔄 Refresh today's data", use_container_width=True):
        with st.spinner("Pulling latest from ERCOT…"):
            try:
                res = DataService(storage, SETTINGS).refresh_today()
                st.sidebar.success(
                    "Refreshed: " + ", ".join(f"{k}={v}" for k, v in res.items())
                )
            except Exception as exc:  # defensive; refresh already swallows most
                st.sidebar.error(f"Refresh hit an error: {exc}")

    st.sidebar.divider()
    st.sidebar.caption(
        f"Rolling window: {SETTINGS.rolling_window_days}d · "
        f"σ threshold: {SETTINGS.sigma_threshold}"
    )
    if not SETTINGS.eia_api_key:
        st.sidebar.info("EIA_API_KEY not set — Henry Hub gas unavailable, "
                        "so implied heat rates will show as n/a.")

    return {"market": market, "market_label": market_label}


# --------------------------------------------------------------------------- #
# panel 1: implied heat rate + band
# --------------------------------------------------------------------------- #
def panel_heat_rate(an: Analytics, market: str) -> None:
    st.subheader("Implied market heat rate  ·  ±2σ rolling band")
    hr = _safe(lambda: an.heat_rate_series(market=market), pd.DataFrame())
    if hr is None or hr.empty:
        st.info("No LMP/gas data cached yet. Use **Refresh today's data** or run "
                "`python -m sparkedge_ercot --backfill`.")
        return

    hubs = st.multiselect(
        "Hubs", [h.label for h in HUBS],
        default=[h.label for h in HUBS], key="hr_hubs",
    )
    fig = go.Figure()
    for hub in hubs:
        sub = hr[hr["hub"] == hub].dropna(subset=["implied_hr"]).sort_values("interval_start")
        if sub.empty:
            continue
        color = HUB_COLORS.get(hub, "#888")
        band = sub.dropna(subset=["upper", "lower"])
        if not band.empty:
            fig.add_trace(go.Scatter(
                x=band["interval_start"], y=band["upper"],
                line=dict(width=0), showlegend=False, hoverinfo="skip",
                name=f"{hub} +2σ",
            ))
            fig.add_trace(go.Scatter(
                x=band["interval_start"], y=band["lower"],
                line=dict(width=0), fill="tonexty",
                fillcolor=_rgba(color, 0.12), showlegend=False,
                hoverinfo="skip", name=f"{hub} -2σ",
            ))
            fig.add_trace(go.Scatter(
                x=band["interval_start"], y=band["hr_mean"],
                line=dict(width=1, dash="dot", color=color),
                opacity=0.6, name=f"{hub} mean", showlegend=False,
                hoverinfo="skip",
            ))
        fig.add_trace(go.Scatter(
            x=sub["interval_start"], y=sub["implied_hr"],
            line=dict(width=2, color=color), name=hub,
            hovertemplate=f"{hub}<br>%{{x}}<br>IHR %{{y:.2f}}<extra></extra>",
        ))
        # mark dislocations
        dis = sub[sub["dislocation"] == True]  # noqa: E712
        if not dis.empty:
            fig.add_trace(go.Scatter(
                x=dis["interval_start"], y=dis["implied_hr"],
                mode="markers", marker=dict(size=9, color=color,
                                            symbol="x", line=dict(width=1)),
                name=f"{hub} dislocation",
                hovertemplate=f"{hub} DISLOCATION<br>%{{x}}<br>IHR %{{y:.2f}}<extra></extra>",
            ))

    fig.update_layout(
        height=430, margin=dict(l=10, r=10, t=10, b=10),
        yaxis_title="Implied HR (MMBtu/MWh)", xaxis_title=None,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


# --------------------------------------------------------------------------- #
# panel 2: live in-the-money strip
# --------------------------------------------------------------------------- #
def panel_money_strip(an: Analytics, market: str) -> None:
    st.subheader("Live spark economics  ·  which units are in the money")
    snap = _safe(lambda: an.latest_snapshot(market=market), pd.DataFrame())
    if snap is None or snap.empty:
        st.info("No snapshot available yet — refresh or backfill data.")
        return

    ts = snap["interval_start"].max()
    st.caption(f"As of interval starting {pd.Timestamp(ts).tz_convert('US/Central'):%Y-%m-%d %H:%M} CT "
               f"· {market.replace('_', ' ').title()}")

    # --- Legend: how to read the lights ---
    st.markdown(
        "<div style='display:flex;gap:18px;align-items:center;flex-wrap:wrap;"
        "font-size:0.82rem;color:#9fb0c3;margin:2px 0 10px;'>"
        "<span>\U0001F7E2 <b style='color:#2ecc71;'>In the money</b> \u2014 profitable to run (positive spark spread)</span>"
        "<span>\U0001F534 <b style='color:#e74c3c;'>Out of the money</b> \u2014 unprofitable (negative spark spread)</span>"
        "<span style='color:#6a7d94;'>A unit clears when its heat rate is <b>below</b> the market\u2019s Implied HR.</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    cols = st.columns(len(snap))
    for col, (_, row) in zip(cols, snap.iterrows()):
        with col:
            hub = row["hub"]
            ihr = row.get("implied_hr")
            ihr_txt = f"{ihr:.2f}" if pd.notna(ihr) else "—"
            gas = row.get("gas_price")
            gas_txt = f"${gas:.2f}" if pd.notna(gas) else "n/a"
            st.markdown(f"### {hub}")
            st.metric("Implied HR", ihr_txt,
                      help=("Implied Heat Rate = power price (LMP) ÷ gas price, in MMBtu/MWh. "
                            "It's the break-even efficiency the market is paying for gas-fired power. "
                            "A generating unit is profitable (\U0001F7E2) when its own heat rate is "
                            "BELOW this number, and unprofitable (\U0001F534) when it's above. "
                            "Lower Implied HR = tougher conditions for gas plants."))
            st.caption(f"LMP ${row.get('lmp', float('nan')):.2f} · Gas {gas_txt}/MMBtu")
            for u in UNIT_CLASSES:
                itm = row.get(f"itm_{u.key}")
                spark = row.get(f"spark_{u.key}")
                if itm is None or pd.isna(itm):
                    badge, cval = "⚪", "—"
                else:
                    badge = "🟢" if itm else "🔴"
                    cval = f"${spark:+.2f}" if pd.notna(spark) else "—"
                st.write(f"{badge} **{u.label}** · {cval}/MWh")
            if bool(row.get("dislocation")):
                z = row.get("hr_z")
                st.warning(f"⚠️ HR dislocation (z = {z:+.1f}σ)")


# --------------------------------------------------------------------------- #
# panel 3: duck curve
# --------------------------------------------------------------------------- #
def panel_duck_curve(an: Analytics) -> None:
    st.subheader("Net-load duck curve  ·  today (evening ramp highlighted)")
    nl = _safe(lambda: an.net_load_today(), pd.DataFrame())
    if nl is None or nl.empty:
        st.info("No load / fuel-mix data cached for today yet.")
        return

    nl = nl.copy()
    nl["local"] = nl["interval_start"].dt.tz_convert("US/Central")
    ramp = _safe(lambda: an.evening_ramp(nl), {})

    fig = go.Figure()

    # Structural evening-ramp window (16:00-21:00 CT) is always shaded as
    # context, so the "evening ramp highlighted" title holds even early in the
    # day before a trough/peak can be detected. The band is clipped to the data
    # actually plotted so it never floats in empty space.
    day0 = nl["local"].dt.normalize().iloc[0]
    ramp_win_start = day0 + pd.Timedelta(hours=16)
    ramp_win_end = day0 + pd.Timedelta(hours=21)
    xmin, xmax = nl["local"].min(), nl["local"].max()
    band_x0 = max(ramp_win_start, xmin)
    band_x1 = min(ramp_win_end, xmax)
    if band_x1 > band_x0:
        fig.add_vrect(x0=band_x0, x1=band_x1, fillcolor="#d62728",
                      opacity=0.08, line_width=0, layer="below",
                      annotation_text="Evening ramp window (16:00–21:00 CT)",
                      annotation_position="top left",
                      annotation_font_size=11, annotation_font_color="#d62728")

    fig.add_trace(go.Scatter(
        x=nl["local"], y=nl["load"], name="Load",
        line=dict(color="#9aa0a6", width=1.5),
    ))
    fig.add_trace(go.Scatter(
        x=nl["local"], y=nl["net_load"], name="Net load (load − solar − wind)",
        line=dict(color="#f58518", width=2.5),
    ))
    # When today's trough->peak ramp is detectable, mark it precisely on top.
    if ramp:
        rs = pd.Timestamp(ramp["ramp_start"]).tz_convert("US/Central")
        re = pd.Timestamp(ramp["ramp_end"]).tz_convert("US/Central")
        fig.add_trace(go.Scatter(
            x=[rs, re],
            y=[ramp["trough_mw"], ramp["peak_mw"]],
            mode="markers+text",
            marker=dict(color="#d62728", size=9, symbol="circle"),
            text=["trough", f"+{ramp['ramp_mw']:,.0f} MW"],
            textposition="top center",
            textfont=dict(color="#d62728", size=11),
            name="Detected ramp",
            showlegend=False,
        ))
    fig.update_layout(
        height=380, margin=dict(l=10, r=10, t=10, b=10),
        yaxis_title="MW", xaxis_title="Time (CT)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)
    if ramp:
        c1, c2, c3 = st.columns(3)
        c1.metric("Ramp magnitude", f"{ramp['ramp_mw']:,.0f} MW")
        c2.metric("Net-load trough", f"{ramp['trough_mw']:,.0f} MW")
        c3.metric("Net-load peak", f"{ramp['peak_mw']:,.0f} MW")


# --------------------------------------------------------------------------- #
# panel 4: alerts
# --------------------------------------------------------------------------- #
def panel_alerts(an: Analytics, market: str) -> None:
    st.subheader("Alerts  ·  current heat-rate dislocations")
    st.caption(
        "Normalized vs. the same hour-of-day over a trailing window, on "
        "log heat rate (HR has a fat right tail from scarcity). Simultaneous "
        "cross-hub breaches are collapsed into one system-level event."
    )
    alerts = _safe(lambda: an.active_alerts(market=market), pd.DataFrame())
    if alerts is None or alerts.empty:
        st.success(f"No dislocations in the last 24h beyond "
                   f"±{SETTINGS.sigma_threshold}σ (hour-conditioned, log HR).")
        return
    disp = alerts.copy()
    disp["interval_start"] = (disp["interval_start"]
                              .dt.tz_convert("US/Central")
                              .dt.strftime("%Y-%m-%d %H:%M"))
    disp["max_abs_z"] = disp["max_abs_z"].round(2)
    disp = disp.rename(columns={
        "interval_start": "Interval (CT)", "direction": "Direction",
        "hubs": "Affected hubs", "n_hubs": "# hubs",
        "max_abs_z": "Peak |z|", "detail": "Detail",
    })
    st.dataframe(disp, use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _safe(fn, default):
    """Run a compute call; on any error show a warning and return default."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Panel degraded — could not compute: {exc}")
        return default


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


# --------------------------------------------------------------------------- #
# main layout
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# header with title + right-aligned ISO logo
# --------------------------------------------------------------------------- #
def _logo_data_uri(filename: str) -> str | None:
    """Return a base64 data URI for a logo in assets/, or None if missing."""
    p = Path(__file__).resolve().parent / "assets" / filename
    if not p.exists():
        return None
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _header(title: str, subtitle: str, logo: str | None = None) -> None:
    uri = _logo_data_uri(logo) if logo else None
    logo_html = (
        f"<img src='{uri}' alt='logo' style='height:64px;width:auto;"
        f"opacity:0.95;filter:drop-shadow(0 0 6px rgba(34,211,238,0.25));'/>"
        if uri else ""
    )
    st.markdown(
        "<div style='display:flex;justify-content:space-between;align-items:center;"
        "gap:16px;margin:-8px 0 4px;'>"
        f"<div><div style='font-size:2.3rem;font-weight:800;line-height:1.1;'>{title}</div>"
        f"<div style='font-size:0.9rem;color:#8fa0b3;margin-top:4px;'>{subtitle}</div></div>"
        f"<div style='flex-shrink:0;'>{logo_html}</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def main() -> None:
    ctrl = sidebar()
    an = get_analytics()
    market = ctrl["market"]

    _header(
        title="Sparkedge ERCOT",
        subtitle="Implied heat rates & spark spreads across ERCOT HB_HOUSTON · HB_NORTH · HB_WEST",
        logo="ercot_logo.png",
    )

    panel_money_strip(an, market)
    st.divider()
    panel_heat_rate(an, market)
    st.divider()
    panel_duck_curve(an)
    st.divider()
    panel_alerts(an, market)


if __name__ == "__main__":
    main()
else:
    # Streamlit executes the module top-to-bottom; call main() on import too.
    main()
