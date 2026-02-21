"""
config.py — Application configuration and .env credential loading.

Provides:
  - AppConfig dataclass used by the scraper and frontend.
  - load_credentials() to read API key and BA login from .env.
  - config_from_json() to deserialise an AppConfig from a JSON file
    (used when the Streamlit UI passes settings to the scraper subprocess).
  - _expand_months() utility shared by the UI and scraper.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Resolve paths relative to this file so it works regardless of cwd
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).resolve().parent
_ENV_PATH = _BASE_DIR / ".env"
DEFAULT_DB_PATH = str(_BASE_DIR / "results.db")


@dataclass
class AppConfig:
    """Centralised application configuration."""

    # Credentials (from .env)
    anthropic_api_key: str
    ba_email: str
    ba_password: str

    # Flight search parameters (set via UI or JSON)
    origin: str
    destinations: list[str]
    months: list[str]  # List of "YYYY-MM" strings covering the date range
    travel_duration_min: int
    travel_duration_max: int
    adults: int
    children: int
    cabin_classes: list[str]

    # Date range boundaries (stored so the JSON round-trip preserves them;
    # months list is the authoritative source used by the scraper)
    start_month: str = ""   # "YYYY-MM" — informational / used to recompute months
    end_month: str = ""     # "YYYY-MM" — informational / used to recompute months

    # Rate-limiting / retry
    delay_min: int = 10
    delay_max: int = 30
    max_retries: int = 2

    # Database path
    db_path: str = field(default_factory=lambda: DEFAULT_DB_PATH)

    def to_json_file(self, path: str | Path) -> None:
        """Serialise this config to a JSON file (for subprocess hand-off)."""
        with open(path, "w") as f:
            json.dump(asdict(self), f)


def _expand_months(start: str, end: str) -> list[str]:
    """Expand 'YYYY-MM' start/end into a list of every month in between (inclusive)."""
    start_date = date.fromisoformat(f"{start}-01")
    end_date = date.fromisoformat(f"{end}-01")
    months: list[str] = []
    current = start_date
    while current <= end_date:
        months.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def load_credentials() -> tuple[str, str, str]:
    """
    Load credentials from .env and return (anthropic_api_key, ba_email, ba_password).

    Raises ValueError if any required variable is missing.
    """
    load_dotenv(_ENV_PATH)

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    ba_email = os.getenv("BA_EMAIL", "")
    ba_password = os.getenv("BA_PASSWORD", "")

    if not anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set in .env")
    if not ba_email or not ba_password:
        raise ValueError("BA_EMAIL and BA_PASSWORD must be set in .env")

    return anthropic_api_key, ba_email, ba_password


def config_from_json(path_or_json: str | Path) -> AppConfig:
    """
    Deserialise an AppConfig from either:
      - a raw JSON string, or
      - a path to a JSON file written by AppConfig.to_json_file().

    After loading the JSON, this function also calls load_dotenv() and
    backfills any empty credential fields from the .env file.  This
    ensures the scraper subprocess always has credentials even if the
    parent process failed to embed them in the JSON.
    """
    import logging

    logger = logging.getLogger("ba_scraper.config")

    text = str(path_or_json).strip()

    # Detect whether the argument is a JSON string or a file path.
    # JSON objects start with '{'; file paths never do.
    if text.startswith("{"):
        logger.info("config_from_json: input looks like a JSON string (%d chars)", len(text))
        raw = text
    else:
        resolved = Path(text).resolve()
        logger.info("config_from_json: input looks like a file path: %s", resolved)
        if not resolved.exists():
            logger.error("Config file does not exist: %s", resolved)
            raise FileNotFoundError(f"Config file not found: {resolved}")
        with open(resolved, "r") as f:
            raw = f.read()

    logger.debug("Raw JSON content (%d chars):\n%s", len(raw), raw)

    data = json.loads(raw)
    logger.info("JSON keys present: %s", list(data.keys()))

    # ------------------------------------------------------------------
    # Load .env so credentials are available in this process too
    # ------------------------------------------------------------------
    logger.info("Loading .env from %s", _ENV_PATH)
    load_dotenv(_ENV_PATH)

    env_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    env_ba_email = os.getenv("BA_EMAIL", "")
    env_ba_password = os.getenv("BA_PASSWORD", "")

    logger.info(".env ANTHROPIC_API_KEY: %s",
                f"present ({len(env_api_key)} chars)" if env_api_key else "EMPTY")
    logger.info(".env BA_EMAIL: %s",
                f"present ({env_ba_email})" if env_ba_email else "EMPTY")
    logger.info(".env BA_PASSWORD: %s",
                f"present ({len(env_ba_password)} chars)" if env_ba_password else "EMPTY")

    # Backfill empty credentials from .env
    if not data.get("anthropic_api_key"):
        logger.warning("JSON anthropic_api_key is empty — backfilling from .env")
        data["anthropic_api_key"] = env_api_key
    if not data.get("ba_email"):
        logger.warning("JSON ba_email is empty — backfilling from .env")
        data["ba_email"] = env_ba_email
    if not data.get("ba_password"):
        logger.warning("JSON ba_password is empty — backfilling from .env")
        data["ba_password"] = env_ba_password

    # Final check — log what we ended up with
    for key in ("anthropic_api_key", "ba_email", "ba_password"):
        val = data.get(key, "")
        if not val:
            logger.error("Config field '%s' is STILL EMPTY after .env backfill", key)
        else:
            logger.info("Config field '%s': present (%d chars)", key, len(val))

    # ------------------------------------------------------------------
    # If months list is missing but start_month/end_month are present,
    # compute it automatically
    # ------------------------------------------------------------------
    if not data.get("months") and data.get("start_month") and data.get("end_month"):
        logger.info("months list missing — computing from start_month=%s end_month=%s",
                     data["start_month"], data["end_month"])
        data["months"] = _expand_months(data["start_month"], data["end_month"])

    # Strip any keys that AppConfig doesn't accept (future-proofing)
    import dataclasses
    valid_fields = {f.name for f in dataclasses.fields(AppConfig)}
    unknown = set(data.keys()) - valid_fields
    if unknown:
        logger.warning("Dropping unknown JSON keys not in AppConfig: %s", unknown)
        for k in unknown:
            del data[k]

    cfg = AppConfig(**data)
    logger.info("AppConfig constructed successfully")
    return cfg
