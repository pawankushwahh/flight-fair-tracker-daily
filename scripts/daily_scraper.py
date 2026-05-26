"""
daily_scraper.py — Runs every day via GitHub Actions.

Logic:
  1. Read route_airlines from Supabase (locked-in config)
  2. For each route, determine which travel dates to scrape today
     using tiered frequency:
       60-90 days out  → every 7 days
       30-60 days out  → every 3 days
       0-30 days out   → every day
  3. NEW DATES: automatically add any travel date that just entered
     the 90-day window (born today = exactly 90 days out)
  4. Scrape Google Flights → write to price_snapshots
  5. Refresh route_summary
  6. Log run to scraper_runs (health check)

Env vars (set in GitHub Actions secrets or .env):
    SUPABASE_URL
    SUPABASE_KEY

Changes from previous version:
  - Replaced brittle CSS selectors in scrape_one() with the same
    heuristic two-strategy extraction used in setup.py
  - Replaced fragile pre-time airline extraction with whitelist normalization
  - Added one-way enforcement (type=2 URL param + UI click fallback)
  - Fixed datetime.utcnow() deprecation → datetime.now(UTC)
  - parse_card() now operates on raw text (not Playwright element)
    so it works identically with both extraction strategies
"""

import os, re, time, random
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
HEADLESS     = True
DELAY_MIN    = 10
DELAY_MAX    = 18
WINDOW_DAYS  = 90

# ── Test mode ─────────────────────────────────────────────────────────────────
# Set TEST_MODE = True to scrape only the nearest 2 travel dates per route.
# Useful for verifying the pipeline end-to-end without a full 90-task run.
# Set back to False (or use env var) for production.
TEST_MODE    = False   # ← flip to False for real runs

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Known airlines whitelist (mirrors setup.py) ───────────────────────────────
# Used to normalize raw scraped text → canonical airline name.
# Keeps frequency counts and matched_airline values consistent between
# setup.py and daily_scraper.py.
KNOWN_AIRLINES = [
    "IndiGo",
    "Air India",
    "Air India Express",
    "Akasa Air",
    "SpiceJet",
    "Vistara",
    "Go First",
    "GoAir",
    "Blue Dart",
    "Alliance Air",
    "Star Air",
    "Fly91",
]

_AIRLINE_PATTERNS = [(a, re.compile(re.escape(a), re.IGNORECASE)) for a in KNOWN_AIRLINES]

# ── Compiled regex constants ──────────────────────────────────────────────────
RE_TIME      = re.compile(r"\b\d{1,2}:\d{2}\s?[AP]M\b")
RE_PRICE_INR = re.compile(r"₹\s?[\d,]+")
RE_DURATION  = re.compile(r"\d+\s?hr(?:\s?\d+\s?min)?|\d+\s?min")
RE_NONSTOP   = re.compile(r"nonstop", re.IGNORECASE)
RE_STOPS     = re.compile(r"(\d+)\s?stop", re.IGNORECASE)
RE_OVERNIGHT = re.compile(r"\+1|\+2", re.IGNORECASE)


# ── Tiered frequency ──────────────────────────────────────────────────────────
def should_scrape_today(travel_date: date) -> bool:
    days_out = (travel_date - date.today()).days
    if days_out < 0:
        return False
    if days_out <= 30:
        return True
    if days_out <= 60:
        return days_out % 3 == 0
    if days_out <= WINDOW_DAYS:
        return days_out % 7 == 0
    return False


def get_dates_to_scrape() -> list[date]:
    today = date.today()
    dates = [today + timedelta(days=d) for d in range(1, WINDOW_DAYS + 1)]
    dates = [d for d in dates if should_scrape_today(d)]
    new_date = today + timedelta(days=WINDOW_DAYS)
    if new_date not in dates:
        dates.append(new_date)
    dates = sorted(dates)
    if TEST_MODE:
        dates = dates[:2]   # only scrape nearest 2 dates for quick testing
    return dates


# ── Load route config ─────────────────────────────────────────────────────────
def load_route_config() -> dict:
    response = sb.table("route_airlines").select("*").eq("active", True).execute()
    config = {}
    for row in response.data:
        rc = row["route_code"]
        if rc not in config:
            config[rc] = {
                "from_iata": row["from_iata"],
                "to_iata":   row["to_iata"],
                "from_city": row.get("from_city", ""),
                "to_city":   row.get("to_city", ""),
                "airlines":  [],
            }
        config[rc]["airlines"].append(row["airline"])
    return config


# ── URL builder ───────────────────────────────────────────────────────────────
def build_url(frm: str, to: str, travel_date: date) -> str:
    # type=2 → One way
    return (
        f"https://www.google.com/travel/flights/search"
        f"?hl=en&gl=IN&curr=INR"
        f"&type=2"
        f"&q=Flights+from+{frm}+to+{to}+on+{travel_date.strftime('%Y-%m-%d')}"
    )


# ── Airline normalization ─────────────────────────────────────────────────────
def normalize_airline(raw_text: str) -> str | None:
    """
    Match raw card text against the known-airlines whitelist.
    Returns canonical name (e.g. "IndiGo") or None if unrecognized.
    Prevents codeshare variants and case differences from splitting counts.
    """
    for canonical, pattern in _AIRLINE_PATTERNS:
        if pattern.search(raw_text):
            return canonical
    return None


# ── Card validation ───────────────────────────────────────────────────────────
def is_valid_card(text: str) -> bool:
    """
    Structural check before parsing. Rejects:
      - Cards with fewer than 2 times (no dep/arr pair)
      - Cards with no INR price (sold-out / ghost entries)
      - Cards with no duration string
      - Overnight/+1-day arrivals (anomalous routings)
    """
    if len(RE_TIME.findall(text)) < 2:
        return False
    if not RE_PRICE_INR.search(text):
        return False
    if not RE_DURATION.search(text):
        return False
    if RE_OVERNIGHT.search(text):
        return False
    return True


# ── Parse card text → structured dict ────────────────────────────────────────
def parse_card_text(text: str) -> dict | None:
    """
    Heuristic parser operating on raw card text — no CSS selectors.
    Returns a dict compatible with the price_snapshots schema, or None.

    Works identically whether the text came from a Playwright element
    or from the JS DOM-walk fallback.
    """
    if not is_valid_card(text):
        return None

    times = RE_TIME.findall(text)
    departure_time = times[0].strip()
    arrival_time   = times[1].strip()

    # Price
    price_raw = RE_PRICE_INR.search(text).group(0)
    price_inr = int(re.sub(r"[^\d]", "", price_raw))

    # Duration → integer minutes
    dur = re.search(r"(\d+)\s*hr\s*(?:(\d+)\s*min)?", text, re.I)
    duration_minutes = None
    if dur:
        duration_minutes = int(dur.group(1)) * 60 + int(dur.group(2) or 0)

    # Stops → integer (0 = nonstop)
    if RE_NONSTOP.search(text):
        stops = 0
    else:
        stop_match = RE_STOPS.search(text)
        stops = int(stop_match.group(1)) if stop_match else 0

    # Layover airport code
    layover_airport = None
    if stops > 0:
        codes = re.findall(r"\b([A-Z]{3})–([A-Z]{3})\b", text)
        if codes:
            layover_airport = codes[0][1]

    # CO2
    co2_match = re.search(r"(\d+)\s*kg\s*CO2", text, re.I)
    co2_kg = int(co2_match.group(1)) if co2_match else None

    co2_vs_avg = None
    avg_match = re.search(
        r"([+\-−]?\s*\d+)\s*%\s*emissions|Avg\s*emissions", text, re.I
    )
    if avg_match:
        if avg_match.group(1):
            co2_vs_avg = float(avg_match.group(1).replace(" ", "").replace("−", "-"))
        else:
            co2_vs_avg = 0.0

    # Trip type
    trip_type = None
    if re.search(r"round\s*trip", text, re.I):
        trip_type = "round_trip"
    elif re.search(r"one\s*way", text, re.I):
        trip_type = "one_way"

    # Airline: whitelist match on text before first time, fallback to full text
    pre_time_text = text.split(times[0])[0]
    airline = normalize_airline(pre_time_text) or normalize_airline(text)

    if not airline:
        return None  # unrecognized airline → discard

    return {
        "airline":          airline,
        "price_inr":        price_inr,
        "departure_time":   departure_time,
        "arrival_time":     arrival_time,
        "duration_minutes": duration_minutes,
        "stops":            stops,
        "layover_airport":  layover_airport,
        "co2_kg":           co2_kg,
        "co2_vs_avg":       co2_vs_avg,
        "trip_type":        trip_type,
    }


# ── DOM extraction strategies (mirrors setup.py) ─────────────────────────────

def _extract_via_broad_selectors(page) -> list[dict]:
    """
    Strategy 1: semantic HTML roles instead of Google's volatile class names.
    Collects all li / [role='listitem'] / article elements, deduplicates by
    text prefix, validates and parses each.
    """
    candidate_selectors = ["li", "[role='listitem']", "article", "div[data-iata]"]
    cards = []
    seen_texts = set()

    for sel in candidate_selectors:
        try:
            elements = page.locator(sel).all()
        except Exception:
            continue

        for el in elements:
            try:
                text = el.inner_text(timeout=1500).strip()
            except Exception:
                continue

            text_key = text[:120]
            if text_key in seen_texts:
                continue
            seen_texts.add(text_key)

            parsed = parse_card_text(text)
            if parsed:
                cards.append(parsed)

    return cards


def _extract_via_js_walk(page) -> list[dict]:
    """
    Strategy 2 (fallback): JS DOM walker injected into the live page.
    Collects text blocks that structurally resemble flight cards regardless
    of class names or surrounding markup. Immune to Google UI refactors.
    """
    try:
        raw_blocks = page.evaluate("""
            () => {
                const blocks = [];
                const seen = new Set();

                function walk(node) {
                    if (node.nodeType === 3) return;
                    const text = node.innerText || '';
                    if (text.length < 30 || text.length > 800) return;

                    const hasPrice    = /₹[\\d,]+/.test(text);
                    const hasTimes    = (text.match(/\\d{1,2}:\\d{2}\\s?[AP]M/g) || []).length >= 2;
                    const hasDuration = /\\d+\\s?hr/.test(text);

                    if (hasPrice && hasTimes && hasDuration) {
                        const key = text.slice(0, 100);
                        if (!seen.has(key)) {
                            seen.add(key);
                            blocks.push(text);
                        }
                        return;
                    }
                    for (const child of node.children) {
                        walk(child);
                    }
                }

                walk(document.body);
                return blocks;
            }
        """)
    except Exception:
        return []

    cards = []
    for text in (raw_blocks or []):
        parsed = parse_card_text(text)
        if parsed:
            cards.append(parsed)
    return cards


def extract_all_flights(page) -> list[dict]:
    """
    Main extraction: try Strategy 1, fall back to Strategy 2.
    Returns all valid parsed flight cards, nonstop first.
    """
    cards = _extract_via_broad_selectors(page)
    if not cards:
        cards = _extract_via_js_walk(page)

    # Nonstop first, then cheapest
    cards.sort(key=lambda c: (c["stops"] > 0, c["price_inr"]))
    return cards


# ── Scrape one route × one travel date ───────────────────────────────────────
def scrape_one(page, frm: str, to: str, travel_date: date,
               target_airlines: list[str]) -> list[dict]:
    """
    Navigate to the route, enforce one-way mode, extract all flight cards,
    then return the cheapest matching card for each target airline.

    Key changes vs original:
      - No longer waits on li.pIav2d / ul.Rk10dc / [jsname='IWWDBc']
      - Uses heuristic two-strategy extraction (broad selectors → JS walk)
      - Airline matching uses whitelist normalization, not raw text slicing
      - One-way enforced via URL param + UI click fallback
    """
    url = build_url(frm, to, travel_date)
    try:
        page.goto(url, timeout=35000, wait_until="domcontentloaded")
    except Exception as e:
        print(f"    ✗ goto: {e}")
        return []

    page.wait_for_timeout(3000)

    # Dismiss consent / cookie dialogs
    for sel in ["button[aria-label='Close']", "button[jsname='VnjF5e']"]:
        try:
            page.locator(sel).first.click(timeout=800)
        except Exception:
            pass

    # Enforce one-way in the UI (type=2 URL param is sometimes ignored
    # by Google when using the ?q= query-string format)
    try:
        trip_type_btn = page.locator(
            "[data-travel-type], [aria-label*='trip'], [aria-label*='Trip']"
        ).first
        label = trip_type_btn.inner_text(timeout=800).strip().lower()
        if "one way" not in label:
            trip_type_btn.click(timeout=1000)
            page.locator(
                "li[data-value='2'], [role='option']:has-text('One way')"
            ).first.click(timeout=1500)
            page.wait_for_timeout(1500)
    except Exception:
        pass  # Already one-way or UI variant without this control

    if "explore" in page.url:
        return []

    # Give the results list a moment to settle
    page.wait_for_timeout(1200)

    all_cards = extract_all_flights(page)

    # For each target airline find the cheapest matching card
    results = []
    for target in target_airlines:
        # normalize_airline on the target itself handles any case variation
        # stored in route_airlines vs what the scraper extracts
        target_canonical = normalize_airline(target) or target
        matches = [
            c for c in all_cards
            if (normalize_airline(c["airline"]) or c["airline"]) == target_canonical
        ]
        if matches:
            best = min(matches, key=lambda x: x["price_inr"])
            best = dict(best)  # copy so we don't mutate the shared list
            best["matched_airline"] = target
            results.append(best)

    return results


# ── Write to Supabase in chunks ───────────────────────────────────────────────
def write_snapshots(rows: list[dict]) -> int:
    """
    Insert price snapshot rows.

    We use plain INSERT (not upsert) because price_snapshots is an append-only
    log — every scrape run produces a new timestamped record. There is no
    meaningful "update existing row" scenario; if we re-scrape the same
    route+airline+travel_date today we simply get a second data point.

    The original on_conflict="route_code,airline,travel_date,scraped_at" failed
    with error 42P10 because Postgres requires an actual UNIQUE INDEX on those
    columns for ON CONFLICT to work, and scraped_at (a full timestamp) makes
    every row unique anyway — so upsert dedup on it is meaningless.

    If your schema DOES have a unique constraint on (route_code, airline,
    travel_date, scraped_date) and you want one-row-per-day semantics, replace
    .insert() with .upsert(..., on_conflict="route_code,airline,travel_date,scraped_date")
    after adding that index in Supabase.
    """
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        sb.table("price_snapshots").insert(chunk).execute()
        written += len(chunk)
    return written


# ── Refresh route_summary via RPC ────────────────────────────────────────────
def refresh_route_summary():
    try:
        sb.rpc("refresh_route_summary", {}).execute()
        print("  ✅ route_summary refreshed")
    except Exception as e:
        print(f"  ⚠  route_summary refresh failed: {e}")


# ── Log scraper run ───────────────────────────────────────────────────────────
def log_run(rows_written, routes_scraped, duration_sec, status="ok", error_msg=None):
    sb.table("scraper_runs").insert({
        "run_date":       date.today().isoformat(),
        "rows_written":   rows_written,
        "routes_scraped": routes_scraped,
        "duration_sec":   round(duration_sec, 2),
        "status":         status,
        "error_msg":      error_msg,
    }).execute()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    start = time.time()
    today = date.today()
    print(f"\n{'='*60}")
    print(f"  Daily Flight Scraper — {today}")
    print(f"{'='*60}")

    route_config = load_route_config()
    if not route_config:
        print("✗  route_airlines is empty — run setup.py first")
        log_run(0, 0, 0, "failed", "route_airlines empty")
        return

    dates_to_scrape = get_dates_to_scrape()
    print(f"  Routes configured : {len(route_config)}")
    print(f"  Travel dates today: {len(dates_to_scrape)}")
    print(f"  New date entering window: {today + timedelta(days=WINDOW_DAYS)}")
    print(f"  Total scrape tasks: ~{len(route_config) * len(dates_to_scrape)}\n")

    total_rows   = 0
    routes_done  = 0
    pending_rows = []
    error_msg    = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        total_tasks = len(route_config) * len(dates_to_scrape)
        task_num = 0

        for route_code, info in route_config.items():
            frm, to  = info["from_iata"], info["to_iata"]
            airlines = info["airlines"]

            for travel_date in dates_to_scrape:
                task_num += 1
                days_out = (travel_date - today).days
                tier = (
                    "daily"  if days_out <= 30 else
                    "3-day"  if days_out <= 60 else
                    "weekly"
                )
                print(
                    f"[{task_num:>4}/{total_tasks}] {route_code}  "
                    f"{travel_date} ({days_out}d out | {tier})",
                    end="  ", flush=True
                )

                try:
                    results = scrape_one(page, frm, to, travel_date, airlines)
                    # timezone-aware UTC timestamp — no deprecation warning
                    now = datetime.now(timezone.utc).isoformat()

                    for r in results:
                        pending_rows.append({
                            "route_code":       route_code,
                            "from_iata":        frm,
                            "to_iata":          to,
                            "from_city":        info["from_city"],
                            "to_city":          info["to_city"],
                            "airline":          r["matched_airline"],
                            "travel_date":      travel_date.isoformat(),
                            "scraped_at":       now,
                            # days_before_travel is a computed column — do NOT send
                            "price_inr":        r["price_inr"],
                            "departure_time":   r["departure_time"],
                            "arrival_time":     r["arrival_time"],
                            "duration_minutes": r["duration_minutes"],
                            "stops":            r["stops"],
                            "layover_airport":  r["layover_airport"],
                            "co2_kg":           r["co2_kg"],
                            "co2_vs_avg":       r["co2_vs_avg"],
                            "trip_type":        r["trip_type"],
                        })

                    print(f"→ {len(results)} prices captured")

                except Exception as e:
                    print(f"→ ERROR: {e}")
                    error_msg = str(e)

                # Flush every 200 rows to avoid losing data on mid-run crash
                if len(pending_rows) >= 200:
                    written = write_snapshots(pending_rows)
                    total_rows += written
                    pending_rows = []

                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            routes_done += 1

        browser.close()

    # Final flush
    if pending_rows:
        total_rows += write_snapshots(pending_rows)

    print(f"\n  Refreshing route_summary …")
    refresh_route_summary()

    duration = time.time() - start
    status   = "ok" if not error_msg else "partial"
    log_run(total_rows, routes_done, duration, status, error_msg)

    print(f"\n{'='*60}")
    print(f"  ✅  Run complete")
    print(f"  Rows written   : {total_rows}")
    print(f"  Routes scraped : {routes_done}")
    print(f"  Duration       : {duration/60:.1f} min")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print(f"\n✗  Fatal error: {e}\n{err}")
        try:
            log_run(0, 0, 0, "failed", str(e)[:500])
        except Exception:
            pass
        raise