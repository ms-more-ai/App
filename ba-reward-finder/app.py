"""
app.py — Streamlit frontend for the BA Reward Flight Finder.

All search settings are configured via the sidebar — no config.yaml needed.
When the user clicks "Run Search", the app builds an AppConfig from the UI
inputs, serialises it to a temp JSON file, and launches scraper.py as a
subprocess.

Displays search results from SQLite with:
  - A summary table (destination, earliest date, lowest Avios, cabins).
  - Per-destination calendar heatmaps (Plotly) coloured by cabin class.
  - Clickable dates linking to the BA search result page.

Launch:  streamlit run app.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import (
    DEFAULT_DB_PATH,
    AppConfig,
    _expand_months,
    load_credentials,
)
from database import fetch_all_results, fetch_run_log, fetch_summary, init_db

# ---------------------------------------------------------------------------
# Page config & DB init
# ---------------------------------------------------------------------------
st.set_page_config(page_title="BA Reward Finder", layout="wide")
init_db(DEFAULT_DB_PATH)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clickable(url: str | None, label: str) -> str:
    if url:
        return f'<a href="{url}" target="_blank">{label}</a>'
    return label


def _cabin_label(cabin: str) -> str:
    return {"economy": "Economy (WT)", "business": "Business (CW)"}.get(cabin, cabin)


# ---------------------------------------------------------------------------
# Calendar heatmap builder (unchanged from before)
# ---------------------------------------------------------------------------

def _build_calendar_heatmap(
    df_dest: pd.DataFrame,
    destination: str,
) -> go.Figure:
    """
    Month-grid calendar heatmap for a single destination.

    Green  = economy only
    Blue   = business only
    Purple = both cabins on the same date
    """
    if df_dest.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{destination} — no results")
        return fig

    df_dest = df_dest.copy()
    df_dest["date"] = pd.to_datetime(df_dest["departure_date"])

    date_cabins: dict[str, set[str]] = {}
    date_avios: dict[str, int | None] = {}
    date_urls: dict[str, str | None] = {}
    for _, row in df_dest.iterrows():
        d = row["departure_date"]
        date_cabins.setdefault(d, set()).add(row["cabin_class"])
        current = date_avios.get(d)
        avios = row["avios_per_person"]
        if current is None or (avios is not None and avios < current):
            date_avios[d] = avios
        if row.get("search_url"):
            date_urls[d] = row["search_url"]

    dates_sorted = sorted(date_cabins.keys())
    if not dates_sorted:
        fig = go.Figure()
        fig.update_layout(title=f"{destination} — no results")
        return fig

    all_dates = []
    for d_str in dates_sorted:
        d = datetime.strptime(d_str, "%Y-%m-%d")
        cabins = date_cabins[d_str]
        if "economy" in cabins and "business" in cabins:
            colour = "#9b59b6"
            cabin_text = "Economy + Business"
        elif "business" in cabins:
            colour = "#3498db"
            cabin_text = "Business (CW)"
        else:
            colour = "#2ecc71"
            cabin_text = "Economy (WT)"

        avios = date_avios.get(d_str)
        avios_text = f"{avios:,} Avios/pp" if avios else "N/A"

        all_dates.append(
            {
                "date": d,
                "date_str": d_str,
                "colour": colour,
                "cabin_text": cabin_text,
                "avios_text": avios_text,
                "url": date_urls.get(d_str, ""),
                "month_label": d.strftime("%b %Y"),
                "dow": d.weekday(),
                "wom": (d.day + d.replace(day=1).weekday() - 1) // 7,
            }
        )

    adf = pd.DataFrame(all_dates)
    months_present = adf["month_label"].unique().tolist()

    fig = go.Figure()
    x_offset = 0
    tick_vals: list[float] = []
    tick_labels: list[str] = []

    for mlabel in months_present:
        mdf = adf[adf["month_label"] == mlabel]
        xs = mdf["dow"] + x_offset
        ys = -mdf["wom"]

        hover = [
            f"{r['date_str']}<br>{r['cabin_text']}<br>{r['avios_text']}"
            for _, r in mdf.iterrows()
        ]

        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text",
                marker=dict(
                    size=28,
                    color=mdf["colour"].tolist(),
                    symbol="square",
                    line=dict(width=1, color="white"),
                ),
                text=[str(d.day) for d in mdf["date"]],
                textfont=dict(color="white", size=10),
                hovertext=hover,
                hoverinfo="text",
                customdata=mdf["url"].tolist(),
                showlegend=False,
            )
        )

        tick_vals.append(x_offset + 3)
        tick_labels.append(mlabel)
        x_offset += 8

    fig.update_layout(
        title=f"{destination} — Reward Availability",
        xaxis=dict(tickvals=tick_vals, ticktext=tick_labels, showgrid=False),
        yaxis=dict(visible=False, showgrid=False),
        height=280,
        margin=dict(l=20, r=20, t=50, b=30),
        plot_bgcolor="white",
    )
    return fig


# ===========================================================================
# Sidebar — search configuration
# ===========================================================================

st.sidebar.header("Search Settings")

# --- Origin ---
origin = st.sidebar.text_input("Origin airport code", value="LHR", max_chars=5)

# --- Destinations (dynamic list) ---
st.sidebar.subheader("Destinations")

if "destinations" not in st.session_state:
    st.session_state.destinations = ["JFK"]

def _add_destination() -> None:
    st.session_state.destinations.append("")

def _remove_destination(idx: int) -> None:
    st.session_state.destinations.pop(idx)

for i, dest_val in enumerate(st.session_state.destinations):
    cols = st.sidebar.columns([3, 1])
    st.session_state.destinations[i] = cols[0].text_input(
        f"Destination {i + 1}",
        value=dest_val,
        max_chars=5,
        key=f"dest_{i}",
        label_visibility="collapsed",
    )
    if len(st.session_state.destinations) > 1:
        cols[1].button(
            "X", key=f"rm_dest_{i}",
            on_click=_remove_destination, args=(i,),
        )

st.sidebar.button("+ Add destination", on_click=_add_destination)

# --- Date range ---
st.sidebar.subheader("Date Range")
today = date.today()
default_start = today.replace(day=1)
# Default end: 6 months from now
if today.month + 6 > 12:
    default_end = today.replace(year=today.year + 1, month=(today.month + 6) - 12, day=1)
else:
    default_end = today.replace(month=today.month + 6, day=1)

start_month = st.sidebar.date_input(
    "Start month",
    value=default_start,
    help="Only the year and month are used",
)
end_month = st.sidebar.date_input(
    "End month",
    value=default_end,
    help="Only the year and month are used",
)

# --- Trip duration ---
st.sidebar.subheader("Trip Duration (days)")
dur_cols = st.sidebar.columns(2)
min_days = dur_cols[0].number_input("Min", min_value=1, max_value=90, value=5)
max_days = dur_cols[1].number_input("Max", min_value=1, max_value=90, value=14)

# --- Passengers ---
st.sidebar.subheader("Passengers")
pax_cols = st.sidebar.columns(2)
adults = pax_cols[0].number_input("Adults", min_value=1, max_value=9, value=2)
children = pax_cols[1].number_input("Children", min_value=0, max_value=9, value=0)

# --- Cabin classes ---
st.sidebar.subheader("Cabin Classes")
search_economy = st.sidebar.checkbox("Economy (World Traveller)", value=True)
search_business = st.sidebar.checkbox("Business (Club World)", value=True)

# ===========================================================================
# Run Search button (sidebar)
# ===========================================================================

if st.sidebar.button("Run Search", type="primary", use_container_width=True):
    # --- Validate inputs ---
    destinations_clean = [d.strip().upper() for d in st.session_state.destinations if d.strip()]
    if not destinations_clean:
        st.sidebar.error("Add at least one destination.")
        st.stop()

    cabin_classes = []
    if search_economy:
        cabin_classes.append("economy")
    if search_business:
        cabin_classes.append("business")
    if not cabin_classes:
        st.sidebar.error("Select at least one cabin class.")
        st.stop()

    start_str = start_month.strftime("%Y-%m")
    end_str = end_month.strftime("%Y-%m")
    if end_str < start_str:
        st.sidebar.error("End month must be on or after start month.")
        st.stop()

    if max_days < min_days:
        st.sidebar.error("Max trip duration must be >= min.")
        st.stop()

    # --- Load credentials from .env ---
    try:
        api_key, ba_email, ba_password = load_credentials()
    except ValueError as exc:
        st.sidebar.error(str(exc))
        st.stop()

    months = _expand_months(start_str, end_str)

    cfg = AppConfig(
        anthropic_api_key=api_key,
        ba_email=ba_email,
        ba_password=ba_password,
        origin=origin.strip().upper() or "LHR",
        destinations=destinations_clean,
        months=months,
        travel_duration_min=int(min_days),
        travel_duration_max=int(max_days),
        adults=int(adults),
        children=int(children),
        cabin_classes=cabin_classes,
        start_month=start_str,
        end_month=end_str,
        db_path=DEFAULT_DB_PATH,
    )

    # --- Write config to a temp file and launch scraper ---
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="ba_cfg_"
    ) as tmp:
        cfg.to_json_file(tmp.name)
        tmp_path = tmp.name

    scraper_path = str(Path(__file__).resolve().parent / "scraper.py")

    with st.spinner(
        f"Searching {len(destinations_clean)} destination(s) "
        f"across {len(months)} month(s) — this may take a while …"
    ):
        result = subprocess.run(
            [sys.executable, scraper_path, "--config-json", tmp_path],
            capture_output=True,
            text=True,
        )

    if result.returncode == 0:
        st.success("Search finished. Results are shown below.")
    else:
        st.error(f"Scraper exited with code {result.returncode}")
        with st.expander("Scraper output"):
            st.code(result.stderr[-3000:] if result.stderr else "(empty)")

    st.rerun()

# ===========================================================================
# Main content area — results display
# ===========================================================================

st.title("BA Reward Flight Finder")
st.caption("Powered by Browser Use + Claude")

st.divider()

# ---- Summary table ----
summary = fetch_summary(DEFAULT_DB_PATH)

if not summary:
    st.info(
        "No results in the database yet. Configure your search in the "
        "sidebar and click **Run Search** to start."
    )
    st.stop()

st.subheader("Summary")
sum_df = pd.DataFrame(summary)
sum_df.columns = ["Destination", "Earliest Date", "Lowest Avios/pp", "Cabins", "Options"]
st.dataframe(sum_df, use_container_width=True, hide_index=True)

st.divider()

# ---- Per-destination calendar heatmaps ----
st.subheader("Availability Calendar")

st.markdown(
    '<span style="color:#2ecc71">&#9632;</span> Economy &nbsp;&nbsp;'
    '<span style="color:#3498db">&#9632;</span> Business &nbsp;&nbsp;'
    '<span style="color:#9b59b6">&#9632;</span> Both',
    unsafe_allow_html=True,
)

all_results = fetch_all_results(DEFAULT_DB_PATH)
df = pd.DataFrame(all_results)

if df.empty:
    st.info("No individual results to display.")
    st.stop()

for dest in sorted(df["destination"].unique()):
    df_dest = df[df["destination"] == dest]
    fig = _build_calendar_heatmap(df_dest, dest)
    st.plotly_chart(fig, use_container_width=True)

    with st.expander(f"{dest} — detail table"):
        detail = df_dest[
            ["departure_date", "cabin_class", "avios_per_person", "seats_available", "search_url"]
        ].copy()
        detail["cabin_class"] = detail["cabin_class"].map(_cabin_label)
        detail["link"] = detail.apply(
            lambda r: _make_clickable(r["search_url"], "Open on BA"), axis=1
        )
        detail = detail.drop(columns=["search_url"])
        detail.columns = ["Date", "Cabin", "Avios/pp", "Seats", "Link"]
        st.markdown(detail.to_html(escape=False, index=False), unsafe_allow_html=True)

st.divider()

# ---- Run log ----
with st.expander("Recent run log"):
    log = fetch_run_log(DEFAULT_DB_PATH)
    if log:
        st.dataframe(pd.DataFrame(log), use_container_width=True, hide_index=True)
    else:
        st.write("No runs logged yet.")
