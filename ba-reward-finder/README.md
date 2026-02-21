# BA Reward Flight Finder

Search British Airways Avios reward flight availability using an AI-driven browser agent ([Browser Use](https://github.com/browser-use/browser-use)) backed by Claude. Results are stored in a local SQLite database and displayed in a Streamlit dashboard with calendar heatmaps.

## Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)
- A British Airways Executive Club account (email + password)

## Setup

```bash
# Clone and enter the project
cd ba-reward-finder

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browsers (required by Browser Use)
playwright install
```

### Environment variables

Copy the example file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
BA_EMAIL=your-email@example.com
BA_PASSWORD=your-password
```

### Configuration

Edit `config.yaml` to set your search parameters:

- **origin** — departure airport (default `LHR`)
- **destinations** — list of IATA airport codes to search
- **date_range** — start/end months in `YYYY-MM` format
- **travel_duration** — min/max days for return trips
- **passengers** — number of adults and children
- **cabin_classes** — `economy` (World Traveller) and/or `business` (Club World)
- **delay** — random pause range (seconds) between agent runs
- **max_retries** — how many times to retry a failed search

## Usage

### Run the scraper

```bash
python scraper.py
```

This iterates over every destination × month defined in `config.yaml`, launching a Browser Use agent for each combination. Results are saved to `results.db`.

### Launch the dashboard

```bash
streamlit run app.py
```

The dashboard shows:

- **Summary table** — earliest date, lowest Avios cost, and available cabins per destination.
- **Calendar heatmaps** — green for economy, blue for business, purple for both. Each date square shows Avios cost on hover.
- **Detail tables** — expandable per-destination tables with direct links to BA search results.
- **Run new search** button — triggers the scraper from the UI.

## Project structure

```
ba-reward-finder/
├── scraper.py        # Browser Use agent orchestration
├── database.py       # SQLite read/write helpers
├── app.py            # Streamlit frontend
├── config.py         # Loads config.yaml and .env
├── config.yaml       # User settings (destinations, dates, passengers)
├── .env              # BA credentials + Anthropic API key (gitignored)
├── .env.example      # Template for .env
├── .gitignore
├── requirements.txt
└── README.md
```

## How it works

The scraper does **not** rely on hard-coded CSS selectors or XPath queries. Instead, it gives the Browser Use agent a natural-language instruction describing what to search for on ba.com. The agent (powered by Claude via Playwright) navigates the site, fills forms, and extracts data autonomously. If BA redesigns their UI, the LLM agent adapts without code changes.

## Error handling

- **Unparseable output** — logged and retried (up to `max_retries`).
- **CAPTCHA / security challenge** — logged as a warning; that search is skipped.
- **Network or agent errors** — retried, then marked as failed in the run log.
- The scraper never crashes the loop on a single failure — it logs the error and moves to the next search.
