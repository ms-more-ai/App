"""
database.py — SQLite persistence for reward-flight search results and run logs.

Tables
------
results    – one row per available flight option (date × cabin × destination).
run_log    – one row per scraper invocation with status and metadata.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

# ---------------------------------------------------------------------------
# Default database path (can be overridden via AppConfig.db_path)
# ---------------------------------------------------------------------------
_DEFAULT_DB = str(Path(__file__).resolve().parent / "results.db")

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------
_CREATE_RESULTS = """
CREATE TABLE IF NOT EXISTS results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    destination     TEXT    NOT NULL,
    origin          TEXT    NOT NULL,
    departure_date  TEXT    NOT NULL,   -- ISO format YYYY-MM-DD
    cabin_class     TEXT    NOT NULL,   -- 'economy' or 'business'
    avios_per_person INTEGER,
    seats_available  INTEGER,
    search_url      TEXT,
    scraped_at      TEXT    NOT NULL,   -- ISO timestamp
    UNIQUE(destination, departure_date, cabin_class)
);
"""

_CREATE_RUN_LOG = """
CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    destination TEXT    NOT NULL,
    month       TEXT    NOT NULL,       -- YYYY-MM
    outcome     TEXT    NOT NULL,       -- 'results_found', 'none_found', 'error'
    detail      TEXT                    -- optional error message or count
);
"""


@contextmanager
def _connect(db_path: str = _DEFAULT_DB) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that yields a connection with WAL mode and row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = _DEFAULT_DB) -> None:
    """Create tables if they don't already exist."""
    with _connect(db_path) as conn:
        conn.execute(_CREATE_RESULTS)
        conn.execute(_CREATE_RUN_LOG)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def upsert_result(
    db_path: str,
    destination: str,
    origin: str,
    departure_date: str,
    cabin_class: str,
    avios_per_person: int | None,
    seats_available: int | None,
    search_url: str | None,
) -> None:
    """Insert or update a single flight-availability result."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO results
                (destination, origin, departure_date, cabin_class,
                 avios_per_person, seats_available, search_url, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(destination, departure_date, cabin_class)
            DO UPDATE SET
                avios_per_person = excluded.avios_per_person,
                seats_available  = excluded.seats_available,
                search_url       = excluded.search_url,
                scraped_at       = excluded.scraped_at
            """,
            (
                destination,
                origin,
                departure_date,
                cabin_class,
                avios_per_person,
                seats_available,
                search_url,
                datetime.utcnow().isoformat(),
            ),
        )


def log_run(
    db_path: str,
    destination: str,
    month: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    """Append one entry to the run_log table."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO run_log (timestamp, destination, month, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), destination, month, outcome, detail),
        )


# ---------------------------------------------------------------------------
# Read helpers (used by the Streamlit frontend)
# ---------------------------------------------------------------------------

def fetch_all_results(db_path: str = _DEFAULT_DB) -> list[dict]:
    """Return every row in the results table as a list of dicts."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM results ORDER BY destination, departure_date"
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_summary(db_path: str = _DEFAULT_DB) -> list[dict]:
    """Per-destination summary: earliest date, lowest Avios, cabin classes available."""
    sql = """
    SELECT
        destination,
        MIN(departure_date)                                 AS earliest_date,
        MIN(avios_per_person)                               AS lowest_avios,
        GROUP_CONCAT(DISTINCT cabin_class)                  AS cabins_available,
        COUNT(*)                                            AS total_options
    FROM results
    GROUP BY destination
    ORDER BY destination
    """
    with _connect(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def fetch_run_log(db_path: str = _DEFAULT_DB, limit: int = 100) -> list[dict]:
    """Return the most recent run-log entries."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
