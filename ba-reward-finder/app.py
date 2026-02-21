"""
app.py — Streamlit frontend for the BA Reward Flight Finder.

Displays search results from SQLite with:
  - A summary table (destination, earliest date, lowest Avios, cabins).
  - Per-destination calendar heatmaps (Plotly) coloured by cabin class.
  - Clickable dates linking to the BA search result page.
  - A "Run new search" button that triggers scraper.py.

Launch:  streamlit run app.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from calendar import monthrange

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import load_config
from database import fetch_all_results, fetch_run_log, fetch_summary, init_db

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="BA Reward Finder", layout="wide")

# ---------------------------------------------------------------------------
# Ensure the DB exists
# ---------------------------------------------------------------------------
_cfg = load_config()
init_db(_cfg.db_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clickable(url: str | None, label: str) -> str:
    """Return an HTML anchor tag if a URL is provided."""
    if url:
        return f'<a href="{url}" target="_blank">{label}</a>'
    return label


def _cabin_colour(cabin: str) -> str:
    """Map cabin class to a display colour."""
    return {"economy": "#2ecc71", "business": "#3498db"}.get(cabin, "#95a5a6")


def _cabin_label(cabin: str) -> str:
    return {"economy": "Economy (WT)", "business": "Business (CW)"}.get(cabin, cabin)


# ---------------------------------------------------------------------------
# Calendar heatmap builder
# ---------------------------------------------------------------------------

def _build_calendar_heatmap(
    df_dest: pd.DataFrame,
    destination: str,
) -> go.Figure:
    """
    Build a month-grid calendar heatmap for a single destination.

    Colour coding:
      Green  = economy only
      Blue   = business only
      Purple = both cabins available on the same date
    """

    if df_dest.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{destination} — no results")
        return fig

    df_dest = df_dest.copy()
    df_dest["date"] = pd.to_datetime(df_dest["departure_date"])

    # Determine colour per date based on which cabins are available
    date_cabins: dict[str, set[str]] = {}
    date_avios: dict[str, int | None] = {}
    date_urls: dict[str, str | None] = {}
    for _, row in df_dest.iterrows():
        d = row["departure_date"]
        date_cabins.setdefault(d, set()).add(row["cabin_class"])
        # Keep the lowest Avios for hover
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

    # Build calendar grid data
    # We'll create a scatter plot positioned by (week_in_month, day_of_week)
    # grouped by month
    all_dates = []
    for d_str in dates_sorted:
        d = datetime.strptime(d_str, "%Y-%m-%d")
        cabins = date_cabins[d_str]
        if "economy" in cabins and "business" in cabins:
            colour = "#9b59b6"  # purple — both
            cabin_text = "Economy + Business"
        elif "business" in cabins:
            colour = "#3498db"  # blue
            cabin_text = "Business (CW)"
        else:
            colour = "#2ecc71"  # green
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
                # x = day of week (Mon=0), y = week-of-month (row)
                "dow": d.weekday(),
                "wom": (d.day + d.replace(day=1).weekday() - 1) // 7,
            }
        )

    adf = pd.DataFrame(all_dates)

    # One subplot per month, arranged horizontally
    months_present = adf["month_label"].unique().tolist()

    fig = go.Figure()

    x_offset = 0
    tick_vals = []
    tick_labels = []

    for mi, mlabel in enumerate(months_present):
        mdf = adf[adf["month_label"] == mlabel]

        # x positions shifted per month block (each block is 7 cols wide + 1 gap)
        xs = mdf["dow"] + x_offset
        ys = -mdf["wom"]  # invert so week 0 is at top

        hover = [
            f"{r['date_str']}<br>{r['cabin_text']}<br>{r['avios_text']}"
            for _, r in mdf.iterrows()
        ]

        # Custom data for click-through URLs
        custom = mdf["url"].tolist()

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
                customdata=custom,
                showlegend=False,
            )
        )

        # Month label position
        tick_vals.append(x_offset + 3)
        tick_labels.append(mlabel)
        x_offset += 8  # 7 days + gap

    fig.update_layout(
        title=f"✈ {destination} — Reward Availability",
        xaxis=dict(
            tickvals=tick_vals,
            ticktext=tick_labels,
            showgrid=False,
        ),
        yaxis=dict(visible=False, showgrid=False),
        height=280,
        margin=dict(l=20, r=20, t=50, b=30),
        plot_bgcolor="white",
    )

    return fig


# ---------------------------------------------------------------------------
# Main UI
# ---------------------------------------------------------------------------

st.title("BA Reward Flight Finder")
st.caption("Powered by Browser Use + Claude")

# ---- Run new search button ----
col_run, col_status = st.columns([1, 3])
with col_run:
    if st.button("Run new search"):
        with col_status:
            with st.spinner("Scraper running — this may take a while …"):
                # Launch scraper as a subprocess so Streamlit doesn't block
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve().parent / "scraper.py")],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    st.success("Scraper finished successfully. Refresh to see new results.")
                else:
                    st.error(f"Scraper exited with code {result.returncode}")
                    with st.expander("Scraper stderr"):
                        st.code(result.stderr[-3000:] if result.stderr else "(empty)")

st.divider()

# ---- Summary table ----
summary = fetch_summary(_cfg.db_path)

if not summary:
    st.info(
        "No results in the database yet. Click **Run new search** to start, "
        "or run `python scraper.py` from the command line."
    )
    st.stop()

st.subheader("Summary")
sum_df = pd.DataFrame(summary)
sum_df.columns = ["Destination", "Earliest Date", "Lowest Avios/pp", "Cabins", "Options"]
st.dataframe(sum_df, use_container_width=True, hide_index=True)

st.divider()

# ---- Per-destination calendar heatmaps ----
st.subheader("Availability Calendar")

# Legend
st.markdown(
    """
    <span style="color:#2ecc71">&#9632;</span> Economy &nbsp;&nbsp;
    <span style="color:#3498db">&#9632;</span> Business &nbsp;&nbsp;
    <span style="color:#9b59b6">&#9632;</span> Both
    """,
    unsafe_allow_html=True,
)

all_results = fetch_all_results(_cfg.db_path)
df = pd.DataFrame(all_results)

if df.empty:
    st.info("No individual results to display.")
    st.stop()

for dest in sorted(df["destination"].unique()):
    df_dest = df[df["destination"] == dest]
    fig = _build_calendar_heatmap(df_dest, dest)
    st.plotly_chart(fig, use_container_width=True)

    # Detail table with clickable links
    with st.expander(f"{dest} — detail table"):
        detail = df_dest[
            ["departure_date", "cabin_class", "avios_per_person", "seats_available", "search_url"]
        ].copy()
        detail["cabin_class"] = detail["cabin_class"].map(
            lambda c: _cabin_label(c)
        )
        detail["link"] = detail.apply(
            lambda r: _make_clickable(r["search_url"], "Open on BA"), axis=1
        )
        detail = detail.drop(columns=["search_url"])
        detail.columns = ["Date", "Cabin", "Avios/pp", "Seats", "Link"]
        st.markdown(detail.to_html(escape=False, index=False), unsafe_allow_html=True)

st.divider()

# ---- Run log ----
with st.expander("Recent run log"):
    log = fetch_run_log(_cfg.db_path)
    if log:
        st.dataframe(pd.DataFrame(log), use_container_width=True, hide_index=True)
    else:
        st.write("No runs logged yet.")
