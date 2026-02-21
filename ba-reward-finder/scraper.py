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
import traceback
from calendar import monthrange
from datetime import datetime
from pathlib import Path

from browser_use import Agent, Browser, BrowserProfile, ChatAnthropic

from config import AppConfig, config_from_json, load_credentials, _expand_months, DEFAULT_DB_PATH
from database import init_db, log_run, upsert_result

# ---------------------------------------------------------------------------
# Logging — use DEBUG so every detail is visible
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ba_scraper")

# Quieten noisy third-party loggers but keep them at INFO
for _lib in ("httpx", "httpcore", "urllib3", "asyncio", "playwright"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Cabin-class display names that BA uses on its website
# ---------------------------------------------------------------------------
CABIN_DISPLAY = {
    "economy": "World Traveller (Economy)",
    "business": "Club World (Business)",
}


# ---------------------------------------------------------------------------
# Credential & config validation
# ---------------------------------------------------------------------------

def _validate_config(cfg: AppConfig) -> None:
    """Log a diagnostic summary of the loaded config and check credentials."""

    logger.info("=" * 60)
    logger.info("CONFIG VALIDATION")
    logger.info("=" * 60)

    # Anthropic key
    key = cfg.anthropic_api_key
    if not key:
        logger.error("ANTHROPIC_API_KEY is EMPTY — agent will fail")
    else:
        logger.info(
            "ANTHROPIC_API_KEY loaded: %s…%s (length %d)",
            key[:8], key[-4:], len(key),
        )

    # BA credentials
    if not cfg.ba_email:
        logger.error("BA_EMAIL is EMPTY — login will fail")
    else:
        logger.info("BA_EMAIL loaded: %s", cfg.ba_email)

    if not cfg.ba_password:
        logger.error("BA_PASSWORD is EMPTY — login will fail")
    else:
        logger.info("BA_PASSWORD loaded: (%d chars, starts with '%s')",
                     len(cfg.ba_password), cfg.ba_password[0])

    # Search params
    logger.info("Origin:        %s", cfg.origin)
    logger.info("Destinations:  %s", cfg.destinations)
    logger.info("Months:        %s", cfg.months)
    logger.info("Duration:      %d–%d days", cfg.travel_duration_min, cfg.travel_duration_max)
    logger.info("Passengers:    %d adults, %d children", cfg.adults, cfg.children)
    logger.info("Cabin classes: %s", cfg.cabin_classes)
    logger.info("Delay:         %d–%d s", cfg.delay_min, cfg.delay_max)
    logger.info("Max retries:   %d", cfg.max_retries)
    logger.info("DB path:       %s", cfg.db_path)
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Task prompt builder
# ---------------------------------------------------------------------------

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

    return f"""Go directly to this URL — the BA Executive Club reward flight search page:
https://www.britishairways.com/travel/flightfinder/execclub/_gf/en_gb?eId=100001

Accept any cookie banners that appear.

If the page asks you to log in (or redirects to a login page), log in with
email "{cfg.ba_email}" and password "{cfg.ba_password}", then return to the
reward flight search page at the URL above.
If you are already logged in, skip the login step.

You should now be on the Avios reward flight search form. Fill in the search
fields with these parameters:
  - From: {cfg.origin}
  - To: {destination}
  - Departure date range: {start_date} to {end_date} ({month_label})
  - Passengers: {cfg.adults} {'adult' if cfg.adults == 1 else 'adults'}{children_clause}
  - Trip type: Return
  - Travel duration: between {cfg.travel_duration_min} and {cfg.travel_duration_max} days

Submit the search and check availability for these cabin classes: {cabin_labels}.

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
- If you see a high demand or queue page on the BA website, wait 30 seconds and
  then refresh the page. Do NOT treat queue pages as CAPTCHAs — they are
  temporary and will resolve on their own after a short wait.
- If a real CAPTCHA or security challenge appears that you cannot solve (e.g. a
  visual puzzle, reCAPTCHA, or "verify you are human" prompt), stop and return
  the text "CAPTCHA_BLOCKED" so the orchestrator can handle it.
- Do NOT navigate away from ba.com.
"""


def _extract_json_from_result(raw: str) -> list[dict] | None:
    """
    Try to pull a JSON array out of the agent's final output.

    The agent *should* return clean JSON, but it sometimes wraps it in
    markdown fences or adds commentary.  This function handles that.
    """
    if not raw:
        logger.debug("_extract_json_from_result: input is empty/None")
        return None

    logger.debug("_extract_json_from_result: raw input (%d chars):\n%s", len(raw), raw)

    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")
    logger.debug("_extract_json_from_result: after stripping fences:\n%s", cleaned)

    # Find the outermost JSON array in the string
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        logger.warning("_extract_json_from_result: no JSON array found in output")
        return None

    json_str = match.group(0)
    logger.debug("_extract_json_from_result: matched JSON (%d chars):\n%s", len(json_str), json_str)

    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            logger.info("_extract_json_from_result: parsed %d items from JSON", len(data))
            return data
        logger.warning("_extract_json_from_result: parsed JSON is not a list, got %s", type(data))
    except json.JSONDecodeError as exc:
        logger.warning("_extract_json_from_result: JSON decode error: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Single search execution
# ---------------------------------------------------------------------------

async def _run_single_search(
    cfg: AppConfig,
    destination: str,
    month: str,
) -> None:
    """Run the Browser Use agent for one (destination, month) combination."""

    task = _build_task_prompt(cfg, destination, month)

    logger.info("-" * 60)
    logger.info("SEARCH START: %s → %s | month=%s", cfg.origin, destination, month)
    logger.debug("Full task prompt:\n%s", task)
    logger.info("-" * 60)

    last_error: str | None = None
    captcha_retries = 0
    max_captcha_retries = 3

    attempt = 0
    while attempt < cfg.max_retries:
        attempt += 1
        logger.info("[Attempt %d/%d] %s → %s %s", attempt, cfg.max_retries,
                     cfg.origin, destination, month)

        try:
            # --- LLM setup ---
            logger.info("  Creating ChatAnthropic LLM (model=claude-sonnet-4-20250514)")
            llm = ChatAnthropic(
                model="claude-sonnet-4-20250514",
                api_key=cfg.anthropic_api_key,
            )
            logger.info("  LLM created successfully")

            # --- Browser setup ---
            logger.info("  Creating Browser with headless=True, viewport=1280x900")
            browser_profile = BrowserProfile(
                headless=True,
                window_size={"width": 1280, "height": 900},
            )
            browser = Browser(browser_profile=browser_profile)
            logger.info("  Browser created successfully")

            # --- Agent setup ---
            logger.info("  Creating Agent (max_failures=5, use_vision=True)")
            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                max_failures=5,
                use_vision=True,
            )
            logger.info("  Agent created successfully")

            # --- Run ---
            logger.info("  Running agent (max_steps=100) …")
            history = await agent.run(max_steps=100)
            logger.info("  Agent run completed")

            # --- Log history summary ---
            logger.info("  Agent finished: is_done=%s", history.is_done())
            logger.info("  Total steps in history: %d", len(history.history))

            # Log each step's extracted content for debugging
            for step_idx, step in enumerate(history.history):
                step_result = step.result if hasattr(step, "result") else None
                if step_result:
                    for action_result in (step_result if isinstance(step_result, list) else [step_result]):
                        content = getattr(action_result, "extracted_content", None)
                        error = getattr(action_result, "error", None)
                        is_done = getattr(action_result, "is_done", False)
                        if content:
                            logger.debug("  Step %d content: %.500s", step_idx, content)
                        if error:
                            logger.warning("  Step %d error: %s", step_idx, error)
                        if is_done:
                            logger.info("  Step %d signalled done", step_idx)

            # --- Extract the final result text ---
            final_text = history.final_result() or ""
            logger.info("  final_result() returned %d chars", len(final_text))
            logger.info("  final_result() content:\n%s", final_text[:2000])

            if not final_text:
                logger.warning("  Agent returned EMPTY final result on attempt %d", attempt)
                last_error = "Agent returned empty final result"
                continue  # retry

            # Check for CAPTCHA signal — retry up to 3 times with 60 s waits
            if "CAPTCHA_BLOCKED" in final_text:
                captcha_retries += 1
                if captcha_retries <= max_captcha_retries:
                    logger.warning(
                        "  CAPTCHA detected for %s %s — waiting 60 s then retrying "
                        "(captcha retry %d/%d)",
                        destination, month,
                        captcha_retries, max_captcha_retries,
                    )
                    await asyncio.sleep(60)
                    attempt -= 1  # don't consume a normal retry for CAPTCHA
                    continue
                else:
                    logger.error(
                        "  CAPTCHA detected for %s %s — all %d captcha retries exhausted",
                        destination, month, max_captcha_retries,
                    )
                    log_run(cfg.db_path, destination, month, "error", "CAPTCHA_BLOCKED")
                    return

            # --- Parse structured results ---
            logger.info("  Parsing JSON from agent output …")
            results = _extract_json_from_result(final_text)

            if results is None:
                logger.warning(
                    "  Could not parse JSON from agent output (attempt %d)",
                    attempt,
                )
                logger.warning("  Raw output was:\n%s", final_text[:2000])
                last_error = f"Unparseable output: {final_text[:500]}"
                continue  # retry

            if len(results) == 0:
                logger.info("  Agent returned empty array — no reward availability for %s %s",
                            destination, month)
                log_run(cfg.db_path, destination, month, "none_found")
                return

            # --- Persist each result row ---
            logger.info("  Saving %d result(s) to database …", len(results))
            for i, row in enumerate(results):
                logger.debug("  Result %d: %s", i, json.dumps(row, default=str))
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

            logger.info("  SUCCESS — saved %d result(s) for %s %s",
                         len(results), destination, month)
            log_run(
                cfg.db_path,
                destination,
                month,
                "results_found",
                f"{len(results)} options",
            )
            return  # success — no need to retry

        except Exception:
            last_error = traceback.format_exc()
            logger.error(
                "  EXCEPTION on attempt %d for %s %s:\n%s",
                attempt,
                destination,
                month,
                last_error,
            )

    # All retries exhausted
    logger.error("  FAILED — all %d attempts exhausted for %s %s",
                 cfg.max_retries, destination, month)
    logger.error("  Last error:\n%s", last_error)
    log_run(cfg.db_path, destination, month, "error", last_error)


# ---------------------------------------------------------------------------
# Orchestration loop
# ---------------------------------------------------------------------------

async def run_all_searches(cfg: AppConfig) -> None:
    """
    Main orchestration loop: iterate over every destination × month,
    running the agent and sleeping between calls.
    """
    _validate_config(cfg)

    logger.info("Initialising database at %s", cfg.db_path)
    init_db(cfg.db_path)

    total = len(cfg.destinations) * len(cfg.months)
    logger.info(
        "Starting scraper — %d destination(s) × %d month(s) = %d total searches",
        len(cfg.destinations),
        len(cfg.months),
        total,
    )

    idx = 0
    for destination in cfg.destinations:
        for month in cfg.months:
            idx += 1
            logger.info("=" * 60)
            logger.info("SEARCH %d / %d", idx, total)
            logger.info("=" * 60)

            await _run_single_search(cfg, destination, month)

            # Polite random delay between runs (skip after the last one)
            if idx < total:
                delay = random.uniform(cfg.delay_min, cfg.delay_max)
                logger.info("Waiting %.1f s before next search …", delay)
                await asyncio.sleep(delay)

    logger.info("=" * 60)
    logger.info("SCRAPER FINISHED — %d searches completed.", total)
    logger.info("=" * 60)


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

    logger.info("Loading config from %s", args.config_json)
    cfg = config_from_json(args.config_json)
    logger.info("Config loaded successfully")

    asyncio.run(run_all_searches(cfg))
