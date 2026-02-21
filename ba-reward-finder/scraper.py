"""
scraper.py — Browser Use agent orchestration for BA reward-flight searches.

For each (destination, month) combination the script:
  1. Builds a natural-language task prompt for the Browser Use agent.
  2. Runs the agent (with up to `max_retries` attempts).
  3. Parses the structured JSON the agent returns.
  4. Writes results to SQLite via database.py.
  5. Logs the outcome and waits a randomised delay before the next run.

Can be invoked directly (`python scraper.py`) or from the Streamlit UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from calendar import monthrange
from datetime import datetime

from browser_use import Agent, Browser, BrowserProfile, ChatAnthropic

from config import AppConfig, config_from_json, load_credentials, _expand_months, DEFAULT_DB_PATH
from database import init_db, log_run, upsert_result

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cabin-class display names that BA uses on its website
# ---------------------------------------------------------------------------
CABIN_DISPLAY = {
    "economy": "World Traveller (Economy)",
    "business": "Club World (Business)",
}


def _build_task_prompt(
    cfg: AppConfig,
    destination: str,
    month: str,
) -> str:
    """Compose the natural-language instruction given to the Browser Use agent."""

    # Human-readable month label, e.g. "March 2026"
    dt = datetime.strptime(month, "%Y-%m")
    month_label = dt.strftime("%B %Y")

    # Last day of the month — so the agent knows the date window
    _, last_day = monthrange(dt.year, dt.month)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    cabin_labels = ", ".join(
        CABIN_DISPLAY.get(c, c) for c in cfg.cabin_classes
    )

    children_clause = ""
    if cfg.children > 0:
        children_clause = f" and {cfg.children} {'child' if cfg.children == 1 else 'children'}"

    return f"""Go to https://www.britishairways.com and accept any cookie banners.

Log in with email "{cfg.ba_email}" and password "{cfg.ba_password}".
If already logged in, skip the login step.

Navigate to the "Book with Avios" or reward flight search page.

Search for **Avios reward flights** (not cash flights) with these parameters:
  - From: {cfg.origin}
  - To: {destination}
  - Departure date range: {start_date} to {end_date} ({month_label})
  - Passengers: {cfg.adults} {'adult' if cfg.adults == 1 else 'adults'}{children_clause}
  - Trip type: Return
  - Travel duration: between {cfg.travel_duration_min} and {cfg.travel_duration_max} days

Check availability for these cabin classes: {cabin_labels}.

For **each available departure date** you find, extract:
  - departure_date (YYYY-MM-DD)
  - cabin_class ("economy" or "business")
  - avios_per_person (integer, the Avios cost per person for that cabin)
  - seats_available (integer, if shown — otherwise null)
  - search_url (the full URL of the search results page)

Return your findings as a **JSON array** and nothing else. Example format:
[
  {{
    "departure_date": "2026-03-15",
    "cabin_class": "economy",
    "avios_per_person": 26000,
    "seats_available": 4,
    "search_url": "https://www.britishairways.com/travel/..."
  }}
]

If there are **no available reward flights** for this route and month, return an
empty JSON array: []

Important:
- Only look at Avios / reward availability, NOT cash fares.
- If a CAPTCHA or security challenge appears that you cannot solve, stop and
  return the text "CAPTCHA_BLOCKED" so the orchestrator can handle it.
- Do NOT navigate away from ba.com.
"""


def _extract_json_from_result(raw: str) -> list[dict] | None:
    """
    Try to pull a JSON array out of the agent's final output.

    The agent *should* return clean JSON, but it sometimes wraps it in
    markdown fences or adds commentary.  This function handles that.
    """
    if not raw:
        return None

    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")

    # Find the outermost JSON array in the string
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        return None

    try:
        data = json.loads(match.group(0))
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    return None


async def _run_single_search(
    cfg: AppConfig,
    destination: str,
    month: str,
) -> None:
    """Run the Browser Use agent for one (destination, month) combination."""

    task = _build_task_prompt(cfg, destination, month)
    logger.info("Starting search: %s → %s for %s", cfg.origin, destination, month)

    last_error: str | None = None

    for attempt in range(1, cfg.max_retries + 1):
        logger.info("  Attempt %d/%d", attempt, cfg.max_retries)

        try:
            # --- Set up the LLM and browser ---
            llm = ChatAnthropic(
                model="claude-sonnet-4-20250514",
                api_key=cfg.anthropic_api_key,
            )

            browser_profile = BrowserProfile(
                headless=True,
                # Use a realistic viewport
                window_width=1280,
                window_height=900,
            )
            browser = Browser(browser_profile=browser_profile)

            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                max_failures=5,
                use_vision=True,
            )

            # Run the agent (allow up to 100 steps for complex navigation)
            history = await agent.run(max_steps=100)

            # --- Extract the final result text ---
            final_text = history.final_result() or ""

            # Check for CAPTCHA signal
            if "CAPTCHA_BLOCKED" in final_text:
                logger.warning(
                    "  CAPTCHA detected for %s %s — skipping", destination, month
                )
                log_run(cfg.db_path, destination, month, "error", "CAPTCHA_BLOCKED")
                return  # Don't retry CAPTCHAs

            # --- Parse structured results ---
            results = _extract_json_from_result(final_text)

            if results is None:
                # Agent returned something unparseable
                logger.warning(
                    "  Could not parse JSON from agent output (attempt %d). Raw: %.300s",
                    attempt,
                    final_text,
                )
                last_error = f"Unparseable output: {final_text[:300]}"
                continue  # retry

            if len(results) == 0:
                logger.info("  No reward availability found for %s %s", destination, month)
                log_run(cfg.db_path, destination, month, "none_found")
                return

            # --- Persist each result row ---
            for row in results:
                upsert_result(
                    db_path=cfg.db_path,
                    destination=destination,
                    origin=cfg.origin,
                    departure_date=row.get("departure_date", ""),
                    cabin_class=row.get("cabin_class", "unknown"),
                    avios_per_person=row.get("avios_per_person"),
                    seats_available=row.get("seats_available"),
                    search_url=row.get("search_url"),
                )

            logger.info(
                "  Saved %d result(s) for %s %s", len(results), destination, month
            )
            log_run(
                cfg.db_path,
                destination,
                month,
                "results_found",
                f"{len(results)} options",
            )
            return  # success — no need to retry

        except Exception as exc:
            last_error = str(exc)
            logger.error(
                "  Agent error on attempt %d for %s %s: %s",
                attempt,
                destination,
                month,
                exc,
            )

    # All retries exhausted
    logger.error("  All %d attempts failed for %s %s", cfg.max_retries, destination, month)
    log_run(cfg.db_path, destination, month, "error", last_error)


async def run_all_searches(cfg: AppConfig) -> None:
    """
    Main orchestration loop: iterate over every destination × month,
    running the agent and sleeping between calls.
    """
    init_db(cfg.db_path)

    total = len(cfg.destinations) * len(cfg.months)
    logger.info(
        "Starting scraper — %d destination(s) × %d month(s) = %d searches",
        len(cfg.destinations),
        len(cfg.months),
        total,
    )

    idx = 0
    for destination in cfg.destinations:
        for month in cfg.months:
            idx += 1
            logger.info("=== Search %d/%d ===", idx, total)

            await _run_single_search(cfg, destination, month)

            # Polite random delay between runs (skip after the last one)
            if idx < total:
                delay = random.uniform(cfg.delay_min, cfg.delay_max)
                logger.info("  Waiting %.1f s before next search …", delay)
                await asyncio.sleep(delay)

    logger.info("Scraper finished — %d searches completed.", total)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BA reward-flight scraper")
    parser.add_argument(
        "--config-json",
        required=True,
        help="Path to a JSON file containing a serialised AppConfig "
             "(produced by AppConfig.to_json_file in the Streamlit UI).",
    )
    args = parser.parse_args()

    cfg = config_from_json(args.config_json)
    asyncio.run(run_all_searches(cfg))
