"""
config.py — Loads config.yaml settings and .env credentials.

Provides a single `AppConfig` dataclass used by the rest of the application.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Resolve paths relative to this file so it works regardless of cwd
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _BASE_DIR / "config.yaml"
_ENV_PATH = _BASE_DIR / ".env"


@dataclass
class AppConfig:
    """Centralised application configuration."""

    # Credentials (from .env)
    anthropic_api_key: str
    ba_email: str
    ba_password: str

    # Flight search parameters (from config.yaml)
    origin: str
    destinations: list[str]
    months: list[str]  # List of "YYYY-MM" strings covering the date range
    travel_duration_min: int
    travel_duration_max: int
    adults: int
    children: int
    cabin_classes: list[str]

    # Rate-limiting / retry
    delay_min: int
    delay_max: int
    max_retries: int

    # Database path
    db_path: str = field(default_factory=lambda: str(_BASE_DIR / "results.db"))


def _expand_months(start: str, end: str) -> list[str]:
    """Expand 'YYYY-MM' start/end into a list of every month in between (inclusive)."""
    start_date = date.fromisoformat(f"{start}-01")
    end_date = date.fromisoformat(f"{end}-01")
    months: list[str] = []
    current = start_date
    while current <= end_date:
        months.append(current.strftime("%Y-%m"))
        # Advance to the first of next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def load_config() -> AppConfig:
    """Read config.yaml and .env, returning a validated AppConfig."""

    # --- Load environment variables ---
    load_dotenv(_ENV_PATH)

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    ba_email = os.getenv("BA_EMAIL", "")
    ba_password = os.getenv("BA_PASSWORD", "")

    if not anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set in .env")
    if not ba_email or not ba_password:
        raise ValueError("BA_EMAIL and BA_PASSWORD must be set in .env")

    # --- Load YAML config ---
    with open(_CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f)

    months = _expand_months(
        cfg["date_range"]["start"],
        cfg["date_range"]["end"],
    )

    return AppConfig(
        anthropic_api_key=anthropic_api_key,
        ba_email=ba_email,
        ba_password=ba_password,
        origin=cfg.get("origin", "LHR"),
        destinations=cfg["destinations"],
        months=months,
        travel_duration_min=cfg["travel_duration"]["min_days"],
        travel_duration_max=cfg["travel_duration"]["max_days"],
        adults=cfg["passengers"]["adults"],
        children=cfg["passengers"]["children"],
        cabin_classes=cfg.get("cabin_classes", ["economy", "business"]),
        delay_min=cfg["delay"]["min_seconds"],
        delay_max=cfg["delay"]["max_seconds"],
        max_retries=cfg.get("max_retries", 2),
    )
