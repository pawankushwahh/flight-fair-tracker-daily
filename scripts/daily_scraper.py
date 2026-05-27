import os, re, time, random
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from supabase import create_client

load_dotenv()

SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
HEADLESS      = True
DELAY_MIN     = 4        # ← was 10
DELAY_MAX     = 7        # ← was 18
WINDOW_DAYS   = 90
TEST_MODE     = False

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

KNOWN_AIRLINES = [
    "IndiGo", "Air India", "Air India Express", "Akasa Air",
    "SpiceJet", "Vistara", "Go First", "GoAir", "Blue Dart",
    "Alliance Air", "Star Air", "Fly91",
]
_AIRLINE_PATTERNS = [(a, re.compile(re.escape(a), re.IGNORECASE)) for a in KNOWN_AIRLINES]

RE_TIME      = re.compile(r"\b\d{1,2}:\d{2}\s?[AP]M\b")
RE_PRICE_INR = re.compile(r"₹\s?[\d,]+")
RE_DURATION  = re.compile(r"\d+\s?hr(?:\s?\d+\s?min)?|\d+\s?min")
RE_NONSTOP   = re.compile(r"nonstop", re.IGNORECASE)
RE_STOPS     = re.compile(r"(\d+)\s?stop", re.IGNORECASE)
RE_OVERNIGHT = re.compile(r"\+1|\+2", re.IGNORECASE)


def should_scrape_today(travel_date: date) -> bool:
    days_out = (travel_date - date.today()).days
    if days_out < 0:
        return False
    if days_out <= 7:       # ← new: every day only for next 7 days
        return True
    if days_out <= 30:      # ← was: every day up to 30
        return days_out % 2 == 0
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
        dates = dates[:2]
    return dates


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


def build_url(frm: str, to: str, travel_date: date) -> str:
    return (
        f"https://www.google.com/travel/flights/search"
        f"?hl=en&gl=IN&curr=INR&type=2"
        f"&q=Flights+from+{frm}+to+{to}+on+{travel_date.strftime('%Y-%m-%d')}"
    )


def normalize_airline(raw_text: str) -> str | None:
    for canonical, pattern in _AIRLINE_PATTERNS:
        if pattern.search(raw_text):
            return canonical
    return None


def is_valid_card(text: str) -> bool:
    if len(RE_TIME.findall(text)) < 2:
        return False
    if not RE_PRICE_INR.search(text):
        return False
    if not RE_DURATION.search(text):
        return False
    if RE_OVERNIGHT.search(text):
        return False
    if re.search(r"round\s*trip", text, re.IGNORECASE):
        return False
    return True


def parse_card_text(text: str) -> dict | None:
    if not is_valid_card(text):
        return None

    times = RE_TIME.findall(text)
    departure_time = times[0].strip()
    arrival_time   = times[1].strip()

    price_raw = RE_PRICE_INR.search(text).group(0)
    price_inr = int(re.sub(r"[^\d]", "", price_raw))

    dur = re.search(r"(\d+)\s*hr\s*(?:(\d+)\s*min)?", text, re.I)
    duration_minutes = None
    if dur:
        duration_minutes = int(dur.group(1)) * 60 + int(dur.group(2) or 0)

    if RE_NONSTOP.search(text):
        stops = 0
    else:
        stop_match = RE_STOPS.search(text)
        stops = int(stop_match.group(1)) if stop_match else 0

    layover_airport = None
    if stops > 0:
        codes = re.findall(r"\b([A-Z]{3})–([A-Z]{3})\b", text)
        if codes:
            layover_airport = codes[0][1]

    co2_match = re.search(r"(\d+)\s*kg\s*CO2", text, re.I)
    co2_kg = int(co2_match.group(1)) if co2_match else None

    co2_vs_avg = None
    avg_match = re.search(r"([+\-−]?\s*\d+)\s*%\s*emissions|Avg\s*emissions", text, re.I)
    if avg_match:
        if avg_match.group(1):
            co2_vs_avg = float(avg_match.group(1).replace(" ", "").replace("−", "-"))
        else:
            co2_vs_avg = 0.0

    trip_type = None
    if re.search(r"round\s*trip", text, re.I):
        trip_type = "round_trip"
    elif re.search(r"one\s*way", text, re.I):
        trip_type = "one_way"

    pre_time_text = text.split(times[0])[0]
    airline = normalize_airline(pre_time_text) or normalize_airline(text)
    if not airline:
        return None

    return {
        "airline": airline, "price_inr": price_inr,
        "departure_time": departure_time, "arrival_time": arrival_time,
        "duration_minutes": duration_minutes, "stops": stops,
        "layover_airport": layover_airport, "co2_kg": co2_kg,
        "co2_vs_avg": co2_vs_avg, "trip_type": trip_type,
    }


def _extract_via_broad_selectors(page) -> list[dict]:
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
                        if (!seen.has(key)) { seen.add(key); blocks.push(text); }
                        return;
                    }
                    for (const child of node.children) walk(child);
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
    cards = _extract_via_broad_selectors(page)
    if not cards:
        cards = _extract_via_js_walk(page)
    cards.sort(key=lambda c: (c["stops"] > 0, c["price_inr"]))
    return cards


def scrape_one(page, frm, to, travel_date, target_airlines):
    url = build_url(frm, to, travel_date)
    try:
        page.goto(url, timeout=35000, wait_until="domcontentloaded")
    except Exception as e:
        print(f"    ✗ goto: {e}")
        return []

    page.wait_for_timeout(3000)

    for sel in ["button[aria-label='Close']", "button[jsname='VnjF5e']"]:
        try:
            page.locator(sel).first.click(timeout=800)
        except Exception:
            pass

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
        pass

    if "explore" in page.url:
        return []

    page.wait_for_timeout(1200)
    all_cards = extract_all_flights(page)

    results = []
    for target in target_airlines:
        target_canonical = normalize_airline(target) or target
        matches = [
            c for c in all_cards
            if (normalize_airline(c["airline"]) or c["airline"]) == target_canonical
        ]
        if matches:
            best = dict(min(matches, key=lambda x: x["price_inr"]))
            best["matched_airline"] = target
            results.append(best)
    return results


def write_snapshots(rows: list[dict]) -> int:
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        sb.table("price_snapshots").insert(chunk).execute()
        written += len(chunk)
    return written


def refresh_route_summary():
    try:
        sb.rpc("refresh_route_summary", {}).execute()
        print("  ✅ route_summary refreshed")
    except Exception as e:
        print(f"  ⚠  route_summary refresh failed: {e}")


def log_run(rows_written, routes_scraped, duration_sec, status="ok", error_msg=None):
    sb.table("scraper_runs").insert({
        "run_date":       date.today().isoformat(),
        "rows_written":   rows_written,
        "routes_scraped": routes_scraped,
        "duration_sec":   round(duration_sec, 2),
        "status":         status,
        "error_msg":      error_msg,
    }).execute()


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

    # ── Option 2: Batch splitting for parallel GitHub Actions jobs ────────────
    batch_index   = int(os.environ.get("BATCH_INDEX", 0))
    total_batches = int(os.environ.get("TOTAL_BATCHES", 1))
    all_routes    = list(route_config.items())
    batch_routes  = all_routes[batch_index::total_batches]
    route_config  = dict(batch_routes)

    # Stagger start to avoid simultaneous Supabase writes
    startup_delay = batch_index * 10
    if startup_delay:
        print(f"  Staggering batch start by {startup_delay}s...")
        time.sleep(startup_delay)

    dates_to_scrape = get_dates_to_scrape()
    print(f"  Batch          : {batch_index + 1}/{total_batches}")
    print(f"  Routes in batch: {len(route_config)}")
    print(f"  Travel dates   : {len(dates_to_scrape)}")
    print(f"  Total tasks    : ~{len(route_config) * len(dates_to_scrape)}\n")

    total_rows   = 0
    routes_done  = 0
    pending_rows = []
    error_msg    = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-setuid-sandbox"],
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
                tier = ("daily" if days_out <= 30 else "3-day" if days_out <= 60 else "weekly")
                print(
                    f"[{task_num:>4}/{total_tasks}] {route_code}  "
                    f"{travel_date} ({days_out}d out | {tier})",
                    end="  ", flush=True
                )

                try:
                    results = scrape_one(page, frm, to, travel_date, airlines)
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

                if len(pending_rows) >= 200:
                    written = write_snapshots(pending_rows)
                    total_rows += written
                    pending_rows = []

                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            routes_done += 1

        browser.close()

    if pending_rows:
        total_rows += write_snapshots(pending_rows)

    # Only batch 0 refreshes route_summary (avoid triple refresh)
    if batch_index == 0:
        print(f"\n  Refreshing route_summary …")
        refresh_route_summary()

    duration = time.time() - start
    log_run(total_rows, routes_done, duration, "ok" if not error_msg else "partial", error_msg)

    print(f"\n{'='*60}")
    print(f"  ✅  Batch {batch_index + 1} complete")
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