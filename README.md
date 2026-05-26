# ✈️ Indian Domestic Flight Price Tracker

A fully automated pipeline that scrapes Google Flights daily for Indian domestic routes, stores structured price history in Supabase, and runs on GitHub Actions — zero infrastructure, zero cost.

---

## What It Does

- Tracks **flight prices across 110+ Indian domestic routes** (all permutations of 11 major airports)
- Scrapes **the top 3 airlines per route** — discovered automatically by `setup.py`
- Uses **tiered scraping frequency** so you capture the full price curve without hammering Google:
  - 60–90 days out → weekly
  - 30–60 days out → every 3 days
  - 0–30 days out → daily
- Automatically **adds new travel dates** as they enter the 90-day window
- Stores every snapshot as an append-only time-series in Supabase
- Refreshes a `route_summary` table after each run for fast aggregation queries
- Logs every run to `scraper_runs` for health monitoring

---

## Tech Stack

| Layer | Tool |
|---|---|
| Scraping | [Playwright](https://playwright.dev/python/) (headless Chromium) |
| Database | [Supabase](https://supabase.com/) (PostgreSQL) |
| Automation | GitHub Actions (cron, `ubuntu-latest`) |
| Language | Python 3.11 |

---

## Project Structure

```
flight-tracker-daily/
├── scripts/
│   ├── setup.py              # Run once: discovers top airlines per route
│   ├── daily_scraper.py      # Runs daily via GitHub Actions
│   └── requirements.txt
├── sql/
│   ├── schema.sql            # Full DB schema — run once in Supabase SQL Editor
│   └── refresh_summary.sql   # RPC function + useful analysis queries
├── github-actions/
│   ├── daily_scraper.yml     # Cron workflow (2 AM IST daily)
│   └── setup.yml             # Manual one-time setup workflow
└── .env.example
```

> **Note:** The `github-actions/` folder contains workflow files. Copy them to `.github/workflows/` in your repo for GitHub Actions to pick them up.

---

## Quick Start

### 1. Clone & set up environment

```bash
git clone https://github.com/YOUR_USERNAME/flight-tracker-daily.git
cd flight-tracker-daily

cp .env.example .env
# Fill in SUPABASE_URL and SUPABASE_KEY
```

### 2. Create the database

In your [Supabase SQL Editor](https://app.supabase.com), run — in order:

```
sql/schema.sql
sql/refresh_summary.sql
```

This creates all tables, indexes, views, and the `refresh_route_summary()` RPC function.

### 3. Install Python dependencies

```bash
pip install -r scripts/requirements.txt
playwright install chromium
```

### 4. Run the one-time setup

```bash
cd scripts
python setup.py
```

This visits all routes on Google Flights across 3 sample dates, identifies the top 3 most-frequently-appearing airlines per route, and saves them to the `route_airlines` table in Supabase.

After it completes, review the `route_airlines` table in your Supabase dashboard. You can deactivate any airline or route by setting `active = false`.

### 5. Test the daily scraper

```bash
python daily_scraper.py
```

Check `TEST_MODE = True` in `daily_scraper.py` to scrape only the 2 nearest dates per route for a quick end-to-end verification before enabling the full daily run.

### 6. Deploy to GitHub Actions

Copy the workflow files to the correct location:

```bash
mkdir -p .github/workflows
cp github-actions/daily_scraper.yml .github/workflows/
cp github-actions/setup.yml .github/workflows/
```

Add your secrets in **GitHub → Settings → Secrets and variables → Actions**:

| Secret | Where to find it |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API → Project URL |
| `SUPABASE_KEY` | Supabase → Project Settings → API → `service_role` key |

Push to GitHub. The scraper will run automatically at **2:00 AM IST every day**.

---

## Database Schema

### Core tables

| Table | Purpose |
|---|---|
| `airports` | 11 major Indian airports with coordinates |
| `route_distances` | Haversine distance (km) for all 110 routes, computed once |
| `route_airlines` | Which airlines to track per route (populated by `setup.py`) |
| `price_snapshots` | Append-only price time-series — the core dataset |
| `route_summary` | Aggregated stats per route, refreshed daily |
| `scraper_runs` | Health log — one row per daily run |

### Useful views

| View | Purpose |
|---|---|
| `latest_prices` | Most recent price per route × airline × travel date |
| `price_curves` | Price vs. days-before-travel (the "will it get cheaper?" view) |
| `airline_comparison` | Head-to-head stats per airline per route |

### Key design decisions

- `price_snapshots` is **append-only** — every scrape adds a new row, enabling full price history and lead-time analysis
- `days_before_travel` is a **computed column** — the DB calculates it automatically, eliminating a common bug source
- `route_summary` uses **UPSERT** (not DELETE + INSERT) so it's never briefly empty during a refresh
- `route_distances` is a **separate geometric fact table** — distance is a property of the airport pair, not the airline config

---

## Airports Covered

| IATA | City |
|---|---|
| DEL | Delhi |
| BOM | Mumbai |
| BLR | Bangalore |
| MAA | Chennai |
| CCU | Kolkata |
| HYD | Hyderabad |
| GOI | Goa |
| COK | Cochin |
| ATQ | Amritsar |
| CCJ | Calicut |
| TRV | Trivandrum |

All 110 directed route pairs (A→B and B→A tracked separately) are covered.

To add or remove airports, edit the `airports` table in Supabase and re-run `setup.py` — no code changes required.

---

## Airlines Tracked

The scraper recognises these Indian carriers:

IndiGo · Air India · Air India Express · Akasa Air · SpiceJet · Vistara · Go First · GoAir · Blue Dart · Alliance Air · Star Air · Fly91

Airlines are matched using a **whitelist normalization** layer, so codeshare variants and case differences never split frequency counts or create duplicate rows.

---

## Analysis Queries

`sql/refresh_summary.sql` includes ready-to-run queries for:

- **Price curves** — how a route's price moves as the travel date approaches
- **Cheapest routes by ₹/km**
- **Best day of week to fly** (needs 30+ days of data)
- **CO₂ efficiency by airline**
- **Health check** — which days had scraper errors
- **Price spike alerts** — routes >40% above their historical average

---

## Scraper Design

### Two-strategy extraction (resilient to Google UI changes)

`daily_scraper.py` doesn't rely on Google's volatile CSS class names. Instead it uses two fallback strategies:

1. **Semantic selectors** — `li`, `[role='listitem']`, `article` — layout-agnostic HTML roles
2. **JS DOM walker** — injected into the live page; collects any text block that structurally looks like a flight card (has ≥2 times, a ₹ price, and a duration string)

If strategy 1 returns nothing, strategy 2 kicks in automatically.

### Card validation

Every parsed card is validated before it's accepted:
- Must have ≥2 time strings (departure + arrival)
- Must have an INR price
- Must have a duration string
- Overnight arrivals (+1/+2 day) are rejected

### Anti-detection

The browser runs with:
- `--disable-blink-features=AutomationControlled`
- Indian locale (`en-IN`) and timezone (`Asia/Kolkata`)
- Realistic user-agent string
- Random 10–18 second delay between requests

---

## GitHub Actions Schedule

```yaml
# Runs at 2:00 AM IST = 8:30 PM UTC (previous day)
- cron: '30 20 * * *'
```

Google Flights prices are freshest in the early morning, making this the optimal scrape time for IST-based routes. The workflow can also be triggered manually from the Actions tab, with an optional dry-run mode.

---

## Environment Variables

```env
SUPABASE_URL=https://xxxxxxxxxxxxxxxxxxxx.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

Copy `.env.example` to `.env` for local runs. For GitHub Actions, set these as repository secrets — never commit `.env` to git (it's in `.gitignore`).

---

## Limitations & Notes

- This project scrapes Google Flights for personal/research use. Review [Google's Terms of Service](https://policies.google.com/terms) before deploying.
- Prices are point-in-time snapshots — not a real-time feed.
- The scraper respects Google's UI; if Google significantly changes its flight results layout, the extraction strategies may need updating.
- `TEST_MODE = True` in `daily_scraper.py` limits each run to 2 dates per route — flip it to `False` for production.

---

## License

MIT — see [LICENSE](LICENSE) for details.