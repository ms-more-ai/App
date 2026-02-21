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

## Usage

### Launch the dashboard

```bash
streamlit run app.py
```

All search settings are configured directly in the Streamlit sidebar:

- **Origin airport** — departure airport code (default `LHR`)
- **Destinations** — add/remove IATA airport codes dynamically
- **Date range** — start and end month pickers
- **Trip duration** — min/max days for return flights
- **Passengers** — number of adults and children
- **Cabin classes** — Economy (World Traveller) and/or Business (Club World)

Click **Run Search** to launch the scraper. Results appear automatically once the search completes.

The dashboard shows:

- **Summary table** — earliest date, lowest Avios cost, and available cabins per destination.
- **Calendar heatmaps** — green for economy, blue for business, purple for both. Each date square shows Avios cost on hover.
- **Detail tables** — expandable per-destination tables with direct links to BA search results.

## Project structure

```
ba-reward-finder/
├── scraper.py        # Browser Use agent orchestration
├── database.py       # SQLite read/write helpers
├── app.py            # Streamlit frontend (search settings in sidebar)
├── config.py         # AppConfig dataclass and .env credential loading
├── .env              # BA credentials + Anthropic API key (gitignored)
├── .env.example      # Template for .env
├── .gitignore
├── requirements.txt
└── README.md
```

## How it works

The scraper does **not** rely on hard-coded CSS selectors or XPath queries. Instead, it gives the Browser Use agent a natural-language instruction describing what to search for on ba.com. The agent (powered by Claude via Playwright) navigates the site, fills forms, and extracts data autonomously. If BA redesigns their UI, the LLM agent adapts without code changes.

When you click **Run Search** in the Streamlit UI, the app:

1. Builds an `AppConfig` from your sidebar inputs and `.env` credentials.
2. Serialises the config to a temporary JSON file.
3. Launches `scraper.py --config-json <path>` as a subprocess.
4. The scraper iterates over every destination × month, running the Browser Use agent for each, writing results to `results.db`.

## Error handling

- **Unparseable output** — logged and retried (up to 2 times per search).
- **CAPTCHA / security challenge** — logged as a warning; that search is skipped.
- **Network or agent errors** — retried, then marked as failed in the run log.
- The scraper never crashes the loop on a single failure — it logs the error and moves to the next search.
