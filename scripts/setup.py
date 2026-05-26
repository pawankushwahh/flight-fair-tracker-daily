"""
setup.py — Run ONCE before starting daily tracking.

What it does:
  1. Visits all routes on Google Flights (across 3 sample dates
     for reliable airline frequency — not just 1 date)
  2. Picks top-3 most-frequently-appearing airlines per route
  3. Saves to Supabase → route_airlines (one row per airline per route)
  4. Prints routes with no flights found so you can skip them

After running:
  - Open Supabase dashboard → route_airlines table and review
  - Then run daily_scraper.py (or trigger via GitHub Actions)

Usage:
    pip install playwright supabase python-dotenv
    playwright install chromium
    cp .env.example .env       # fill in SUPABASE_URL and SUPABASE_KEY
    python setup.py

Changes from previous version:
  - Replaced brittle CSS-class selectors with heuristic text-based parsing
  - Added structural card validation (times, price, stops, duration)
  - Added known Indian airline whitelist for disambiguation
  - Added nonstop-only prioritization
  - Added ghost/sold-out/codeshare filtering
  - Improved fallback: tries JS-injection DOM walk if no cards via selectors
"""

import os, re, time, random, math, json
from datetime import date, timedelta
from itertools import permutations
from collections import Counter

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Airport loader ────────────────────────────────────────────────────────────
# Airports are loaded from Supabase at runtime instead of being hardcoded.
# To add/remove airports just edit the `airports` table in Supabase —
# no code change needed.
#
# Expected columns (must exist in your airports table):
#   iata   TEXT  — 3-letter IATA code          e.g. "DEL"
#   city   TEXT  — human-readable city name    e.g. "Delhi"
#   active BOOL  — set False to skip an airport without deleting it
#   lat    FLOAT — latitude  (optional, used for haversine distance)
#   lon    FLOAT — longitude (optional, used for haversine distance)
#
# If your table uses different column names, adjust the mapping in
# load_airports() below — everything else in the script is untouched.

def load_airports() -> list[dict]:
    """
    Fetch active airports from Supabase → airports table.
    Returns a list of dicts with keys: iata, city, lat, lon
    (same shape the rest of the script expects).

    Falls back to an empty list and prints a clear error if the
    table doesn't exist or the query fails, so setup.py exits
    gracefully rather than crashing mid-run.
    """
    try:
        resp = sb.table("airports").select("*").eq("active", True).execute()
    except Exception as e:
        print(f"✗  Could not load airports from Supabase: {e}")
        return []

    airports = []
    for row in resp.data:
        # Normalize column names — handles minor variations
        # (e.g. "iata_code" vs "iata", "city_name" vs "city")
        iata = row.get("iata") or row.get("iata_code") or row.get("code")
        city = row.get("city") or row.get("city_name") or row.get("name")
        lat  = row.get("lat")  or row.get("latitude")  or 0.0
        lon  = row.get("lon")  or row.get("longitude") or row.get("lng") or 0.0

        if not iata or not city:
            print(f"  ⚠  Skipping airport row with missing iata/city: {row}")
            continue

        airports.append({
            "iata": iata.strip().upper(),
            "city": city.strip(),
            "lat":  float(lat),
            "lon":  float(lon),
        })

    return airports

HEADLESS  = True
DELAY_MIN = 10
DELAY_MAX = 18
TOP_N     = 3   # airlines to track per route

SAMPLE_DATES = [
    (date.today() + timedelta(days=d)).strftime("%Y-%m-%d")
    for d in [10, 21, 35]
]

# ── Known Indian carriers — used as a whitelist to anchor fuzzy extraction ────
# If text from a card contains one of these (case-insensitive), it's used
# as the canonical airline name instead of the raw extracted text.
# This prevents codeshare variants like "IndiGo operated by IndiGo" from
# polluting the frequency counter with near-duplicate strings.
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

# Pre-compiled for speed
_AIRLINE_PATTERNS = [(a, re.compile(re.escape(a), re.IGNORECASE)) for a in KNOWN_AIRLINES]

# ── Regex patterns used in heuristic card parsing ─────────────────────────────
RE_TIME      = re.compile(r"\b\d{1,2}:\d{2}\s?[AP]M\b")
RE_PRICE_INR = re.compile(r"₹\s?[\d,]+")
RE_DURATION  = re.compile(r"\d+\s?hr(?:\s?\d+\s?min)?|\d+\s?min")
RE_NONSTOP   = re.compile(r"nonstop", re.IGNORECASE)
RE_STOPS     = re.compile(r"(\d+)\s?stop", re.IGNORECASE)
RE_OVERNIGHT = re.compile(r"\+1|\+2", re.IGNORECASE)   # "+1 day" arrivals


# ── Helpers ───────────────────────────────────────────────────────────────────
def haversine_km(a, b):
    R = 6371
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return round(2 * R * math.asin(math.sqrt(h)), 1)


def build_url(frm, to, travel_date):
    # type=2 → One way  (1 = Round trip, 3 = Multi-city)
    return (
        f"https://www.google.com/travel/flights/search"
        f"?hl=en&gl=IN&curr=INR"
        f"&type=2"
        f"&q=Flights+from+{frm}+to+{to}+on+{travel_date}"
    )


def normalize_airline(raw_text):
    """
    Given raw text scraped from a card, return a canonical airline name
    by matching against the known-airlines whitelist.

    Rationale: Google Flights sometimes shows "IndiGo", "INDIGO", or even
    "IndiGo operated by IndiGo" depending on UI variant. Normalizing here
    ensures the frequency counter doesn't split counts across variants.

    Returns None if no known airline is matched (guards against noise).
    """
    for canonical, pattern in _AIRLINE_PATTERNS:
        if pattern.search(raw_text):
            return canonical
    return None


# def is_valid_card(card_text):
   
    # times = RE_TIME.findall(card_text)
    # if len(times) < 2:
    #     return False
    # if not RE_PRICE_INR.search(card_text):
    #     return False
    # if not RE_DURATION.search(card_text):
    #     return False
    # if RE_OVERNIGHT.search(card_text):
    #     return False
    # return True

def is_valid_card(card_text):

    """
    Structural validation of a candidate flight card's text content.

    A valid card must have:
      - At least two times (departure + arrival)
      - A price in INR
      - A duration string
      - NOT be a sold-out / ghost entry (no price = sold out)
      - NOT be an overnight anomaly (+1 day)

    Rationale: Ghost entries (sold-out, unavailable) still render in the DOM
    with times but no price. Filtering here avoids counting phantom flights.
    Overnight/+1 arrivals are excluded per spec — they're typically long
    indirect routings or anomalous fares.
    """
      

    times = RE_TIME.findall(card_text)
    if len(times) < 2:
        return False
    if not RE_PRICE_INR.search(card_text):
        return False
    if not RE_DURATION.search(card_text):
        return False
    if RE_OVERNIGHT.search(card_text):
        return False
    if re.search(r"round\s*trip", card_text, re.IGNORECASE):  # ← ADD THIS
        return False
    return True


def is_nonstop(card_text):
    """
    Returns True if the card text explicitly says 'Nonstop'.

    Rationale: Per the spec, nonstop flights are prioritized.
    We don't hard-exclude multi-stop flights in case a route has no nonstop
    options (e.g. smaller cities), but nonstop cards are flagged so the
    caller can weight them higher.
    """
    return bool(RE_NONSTOP.search(card_text))


def parse_card_text(card_text):
    """
    Parse a single flight card's raw text into a structured dict.

    Strategy:
      - Find the first time (departure), second time (arrival)
      - Find price, duration, stops
      - Identify airline by whitelist match against the block before the first time

    Returns a dict or None if parsing fails basic validation.

    This function does NOT rely on any CSS class names. It only looks at the
    visible text content, making it resilient to Google Flights UI refactors.
    """
    if not is_valid_card(card_text):
        return None

    times = RE_TIME.findall(card_text)
    departure = times[0].strip()
    arrival   = times[1].strip()

    price_match = RE_PRICE_INR.search(card_text)
    price_raw   = price_match.group(0) if price_match else ""
    price_num   = int(re.sub(r"[^\d]", "", price_raw)) if price_raw else None

    duration_match = RE_DURATION.search(card_text)
    duration = duration_match.group(0).strip() if duration_match else ""

    if RE_NONSTOP.search(card_text):
        stops = "Nonstop"
    else:
        stop_match = RE_STOPS.search(card_text)
        stops = f"{stop_match.group(1)} stop" if stop_match else "Unknown"

    # Airline: look in text that appears before the first time string.
    # This is where Google Flights consistently places the airline name
    # (logo alt text or visible label), regardless of surrounding markup.
    pre_time_text = card_text.split(times[0])[0]
    airline = normalize_airline(pre_time_text)

    # Fallback: search entire card text if not found before the time
    if not airline:
        airline = normalize_airline(card_text)

    if not airline:
        return None  # Cannot identify airline → discard card

    return {
        "airline":   airline,
        "price":     price_num,
        "departure": departure,
        "arrival":   arrival,
        "duration":  duration,
        "stops":     stops,
        "nonstop":   stops == "Nonstop",
    }


# ── Core DOM extraction strategies ───────────────────────────────────────────

def _extract_via_broad_selectors(page):
    """
    Strategy 1: Broad structural selectors.

    Instead of targeting Google's specific (volatile) class names,
    we target generic list/article patterns that flight results
    have consistently used. We collect ALL candidate elements and
    then filter by card validity, rather than trusting a single selector.

    Why this is more stable:
      - li, [role='listitem'], article are semantically stable HTML roles
      - Google changes class names but rarely changes element roles
      - We validate content, not container identity
    """
    candidate_selectors = [
        "li",
        "[role='listitem']",
        "article",
        "div[data-iata]",  # sometimes present on newer UI variants
    ]

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

            # Deduplicate: nested elements return the same text as parents
            text_key = text[:120]
            if text_key in seen_texts:
                continue
            seen_texts.add(text_key)

            parsed = parse_card_text(text)
            if parsed:
                cards.append(parsed)

    return cards


def _extract_via_js_walk(page):
    """
    Strategy 2: JavaScript DOM walk (fallback).

    If selector-based extraction finds nothing, we inject a small JS
    snippet that walks the live DOM and collects leaf-node text blocks
    that look like flight cards (contain a price, two times, a duration).

    Why this works as a fallback:
      - JS runs in the page context, unaffected by Playwright selector engine
      - It reads the rendered DOM directly, so it works regardless of
        whatever class names or structure Google uses
      - We return raw text blocks and parse them with the same heuristics
    """
    try:
        raw_blocks = page.evaluate("""
            () => {
                const blocks = [];
                const seen = new Set();

                function walk(node) {
                    if (node.nodeType === 3) return;  // text node
                    const text = node.innerText || '';
                    if (text.length < 30 || text.length > 800) return;

                    // Must look like a flight card
                    const hasPrice    = /₹[\\d,]+/.test(text);
                    const hasTimes    = (text.match(/\\d{1,2}:\\d{2}\\s?[AP]M/g) || []).length >= 2;
                    const hasDuration = /\\d+\\s?hr/.test(text);

                    if (hasPrice && hasTimes && hasDuration) {
                        const key = text.slice(0, 100);
                        if (!seen.has(key)) {
                            seen.add(key);
                            blocks.push(text);
                        }
                        return;  // don't descend further into valid card
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


def extract_flights_from_page(page):
    """
    Main extraction entry point. Tries Strategy 1, falls back to Strategy 2.

    Returns a list of parsed flight card dicts.
    Nonstop flights are sorted first per the spec requirement to prioritize them.
    """
    cards = _extract_via_broad_selectors(page)

    if not cards:
        cards = _extract_via_js_walk(page)

    if not cards:
        return []

    # Sort: nonstop first, then by price ascending (most representative fares first)
    cards.sort(key=lambda c: (not c["nonstop"], c["price"] or 999999))

    return cards


def extract_airlines_from_page(page):
    """
    Public interface used by scrape_route_airlines — identical signature
    to the original function so downstream code is unaffected.

    Returns a list of airline name strings (may have duplicates — the
    caller aggregates into a Counter for frequency ranking).

    Change from original:
      - No longer relies on li.pIav2d / ul.Rk10dc / [jsname='IWWDBc']
      - Instead uses heuristic text parsing + whitelist normalization
      - Nonstop airlines are double-counted to weight them higher in the
        frequency ranking (they are the preferred tracking targets per spec)
    """
    cards = extract_flights_from_page(page)

    airlines = []
    for card in cards:
        airline = card["airline"]
        airlines.append(airline)
        # Weight nonstop flights double: they're the primary tracking targets.
        # This ensures a nonstop carrier beats a connecting carrier even if
        # the connecting carrier appears on more total cards.
        if card["nonstop"]:
            airlines.append(airline)

    return airlines


# ── Navigation ────────────────────────────────────────────────────────────────

def scrape_route_airlines(page, frm, to, travel_date):
    """Navigate to route, enforce one-way mode, dismiss dialogs, extract."""
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

    # Enforce one-way in the UI.
    # type=2 in the URL is usually respected, but Google occasionally ignores
    # the param when using the ?q= query format and renders a round-trip form.
    # We check the trip-type control and click "One way" if it is not already set.
    try:
        # Look for the trip-type button that does NOT already say "One way"
        trip_type_btn = page.locator("[data-travel-type], [aria-label*='trip'], [aria-label*='Trip']").first
        label = trip_type_btn.inner_text(timeout=800).strip().lower()
        if "one way" not in label:
            trip_type_btn.click(timeout=1000)
            # After opening the dropdown, select the "One way" option
            page.locator("li[data-value='2'], [role='option']:has-text('One way')").first.click(timeout=1500)
            page.wait_for_timeout(1500)  # let results reload
    except Exception:
        pass  # Already one-way, or UI variant without this control — proceed

    if "explore" in page.url:
        return []

    return extract_airlines_from_page(page)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Flight Tracker — One-Time Setup")
    print("=" * 60)

    airports = load_airports()
    if not airports:
        print("✗  No active airports found in Supabase → airports table.")
        print("   Add rows to the airports table and set active=true, then re-run.")
        return

    routes = list(permutations(airports, 2))
    print(f"  Airports : {len(airports)}")
    print(f"  Routes   : {len(routes)}")
    print(f"  Checking dates: {SAMPLE_DATES}")
    est_min = len(routes) * len(SAMPLE_DATES) * ((DELAY_MIN + DELAY_MAX) / 2 + 5) / 60
    print(f"  Estimated time: ~{est_min:.0f} minutes\n")

    # route_code → Counter of airline appearances across all sample dates
    airline_counters = {}

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

        for i, (a, b) in enumerate(routes, 1):
            frm, to = a["iata"], b["iata"]
            route_code = f"{frm}-{to}"
            counter = Counter()

            for travel_date in SAMPLE_DATES:
                print(f"  [{i:>3}/{len(routes)}] {route_code}  {travel_date}", end="  ", flush=True)
                found = scrape_route_airlines(page, frm, to, travel_date)
                counter.update(found)
                print(f"→ {len(found)} airline signals")
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            airline_counters[route_code] = (a, b, counter)

        browser.close()

    # ── Pick top N airlines and write to Supabase ─────────────────────────────
    print(f"\nWriting to Supabase → route_airlines …")
    dead_routes = []
    rows_written = 0

    for route_code, (a, b, counter) in airline_counters.items():
        top_airlines = [airline for airline, _ in counter.most_common(TOP_N)]

        if not top_airlines:
            dead_routes.append(route_code)
            continue

        for rank, airline in enumerate(top_airlines, 1):
            sb.table("route_airlines").upsert({
                "route_code": route_code,
                "from_iata":  a["iata"],
                "to_iata":    b["iata"],
                "from_city":  a["city"],
                "to_city":    b["city"],
                "airline":    airline,
                "rank":       rank,
                "active":     True,
            }, on_conflict="route_code,airline").execute()
            rows_written += 1

    print(f"✅  Done! {rows_written} airline-route rows written.")
    print(f"\nRoutes with NO flights found ({len(dead_routes)}) — will be skipped:")
    for rc in dead_routes:
        print(f"   • {rc}")
    print(f"\n👉  Review Supabase → route_airlines, then start daily_scraper.py")


if __name__ == "__main__":
    main()