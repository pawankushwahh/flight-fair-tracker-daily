-- ============================================================
-- Indian Domestic Flight Price Tracker — Supabase Schema
-- Run this ONCE in Supabase SQL Editor
-- ============================================================


-- ── 1. Airports ───────────────────────────────────────────────
-- FIX: Corrected to your actual 11 large airports (ATQ/CCJ/TRV
--      not AMD/PNQ/JAI which were in the wrong version)
CREATE TABLE IF NOT EXISTS airports (
    iata  CHAR(3)       PRIMARY KEY,
    city  TEXT          NOT NULL,
    name  TEXT          NOT NULL,
    lat   NUMERIC(8,5)  NOT NULL,
    lon   NUMERIC(8,5)  NOT NULL
);

INSERT INTO airports (iata, city, name, lat, lon) VALUES
    ('ATQ', 'Amritsar',   'Sri Guru Ram Dass Jee International',  31.71000, 74.79700),
    ('BLR', 'Bangalore',  'Kempegowda International',             13.19800, 77.70600),
    ('COK', 'Cochin',     'Cochin International',                 10.15200, 76.40200),
    ('GOI', 'Goa',        'Goa International',                    15.38100, 73.83100),
    ('DEL', 'Delhi',      'Indira Gandhi International',          28.56700, 77.10300),
    ('HYD', 'Hyderabad',  'Rajiv Gandhi International',           17.23100, 78.43000),
    ('CCJ', 'Calicut',    'Calicut International',                11.13700, 75.95500),
    ('CCU', 'Kolkata',    'Netaji Subhas Chandra Bose Intl',      22.65500, 88.44700),
    ('MAA', 'Chennai',    'Chennai International',                12.99400, 80.18100),
    ('BOM', 'Mumbai',     'Chhatrapati Shivaji Maharaj Intl',     19.08900, 72.86800),
    ('TRV', 'Trivandrum', 'Thiruvananthapuram International',      8.48200, 76.92000)
ON CONFLICT (iata) DO NOTHING;


-- ── 2. Haversine distance function ───────────────────────────
-- FIX: Added this — route_distances needs it, and refresh_summary
--      was previously getting distance from route_airlines (unreliable)
CREATE OR REPLACE FUNCTION haversine_km(
    lat1 NUMERIC, lon1 NUMERIC,
    lat2 NUMERIC, lon2 NUMERIC
) RETURNS NUMERIC AS $$
DECLARE
    r    NUMERIC := 6371;
    dlat NUMERIC := RADIANS(lat2 - lat1);
    dlon NUMERIC := RADIANS(lon2 - lon1);
    a    NUMERIC;
BEGIN
    a := SIN(dlat/2)^2
       + COS(RADIANS(lat1)) * COS(RADIANS(lat2)) * SIN(dlon/2)^2;
    RETURN ROUND(r * 2 * ASIN(SQRT(a)), 1);
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- ── 3. Route distances (computed once from airport coords) ────
-- FIX: Added as a proper separate table.
--      Previously distance lived in route_airlines which is config,
--      not a good place for a geometric fact.
CREATE TABLE IF NOT EXISTS route_distances (
    route_code   TEXT         PRIMARY KEY,
    from_iata    CHAR(3)      NOT NULL REFERENCES airports(iata),
    to_iata      CHAR(3)      NOT NULL REFERENCES airports(iata),
    distance_km  NUMERIC(8,1) NOT NULL
);

-- Populate all 110 routes automatically
INSERT INTO route_distances (route_code, from_iata, to_iata, distance_km)
SELECT
    a1.iata || '-' || a2.iata,
    a1.iata,
    a2.iata,
    haversine_km(a1.lat, a1.lon, a2.lat, a2.lon)
FROM airports a1
CROSS JOIN airports a2
WHERE a1.iata != a2.iata
ON CONFLICT (route_code) DO NOTHING;


-- ── 4. Route-airline config ────────────────────────────────────
-- FIX: Changed from 3-column design (airline_1/2/3) to one row
--      per airline. Makes querying far cleaner:
--        "which routes does IndiGo operate?" = simple WHERE clause
--        vs checking 3 columns every time.
-- Populated by setup.py, updated manually only if airline drops a route.
CREATE TABLE IF NOT EXISTS route_airlines (
    id          SERIAL       PRIMARY KEY,
    route_code  TEXT         NOT NULL,
    from_iata   CHAR(3)      NOT NULL REFERENCES airports(iata),
    to_iata     CHAR(3)      NOT NULL REFERENCES airports(iata),
    from_city   TEXT,
    to_city     TEXT,
    airline     TEXT         NOT NULL,
    rank        SMALLINT,        -- 1 = most frequent on this route
    active      BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (route_code, airline)
);

CREATE INDEX IF NOT EXISTS idx_ra_route  ON route_airlines(route_code);
CREATE INDEX IF NOT EXISTS idx_ra_active ON route_airlines(active) WHERE active = TRUE;


-- ── 5. Price snapshots (core time-series — NEVER delete rows) ──
CREATE TABLE IF NOT EXISTS price_snapshots (
    id              BIGSERIAL    PRIMARY KEY,
    route_code      TEXT         NOT NULL,
    from_iata       CHAR(3)      NOT NULL REFERENCES airports(iata),
    to_iata         CHAR(3)      NOT NULL REFERENCES airports(iata),
    from_city       TEXT,
    to_city         TEXT,
    airline         TEXT         NOT NULL,
    travel_date     DATE         NOT NULL,
    scraped_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- FIX: Computed column — DB calculates this automatically,
    --      scraper no longer needs to send it (removes a bug source)
    days_before_travel INT GENERATED ALWAYS AS (
        (travel_date - scraped_at::date)
    ) STORED,

    price_inr       INT          NOT NULL CHECK (price_inr > 0),
    departure_time  TEXT,
    arrival_time    TEXT,
    duration_minutes INT         CHECK (duration_minutes > 0),

    -- FIX: INT not TEXT — so you can query: WHERE stops = 0
    stops           INT          NOT NULL DEFAULT 0,
    layover_airport CHAR(3),

    co2_kg          INT,

    -- FIX: NUMERIC not TEXT — so you can query: WHERE co2_vs_avg < -15
    --      NULL = not available, 0 = average, negative = cleaner than avg
    co2_vs_avg      NUMERIC(5,1),

    trip_type       TEXT,

    -- Prevent duplicate scrapes: same route+airline+date on same calendar day
    UNIQUE (route_code, airline, travel_date, (scraped_at::date))
);

CREATE INDEX IF NOT EXISTS idx_ps_route_airline_date
    ON price_snapshots (route_code, airline, travel_date);
CREATE INDEX IF NOT EXISTS idx_ps_travel_date
    ON price_snapshots (travel_date);
CREATE INDEX IF NOT EXISTS idx_ps_scraped_at
    ON price_snapshots (scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_ps_days_before
    ON price_snapshots (days_before_travel);
CREATE INDEX IF NOT EXISTS idx_ps_price
    ON price_snapshots (price_inr);


-- ── 6. Route summary (refreshed after each daily scrape run) ──
CREATE TABLE IF NOT EXISTS route_summary (
    route_code        TEXT         PRIMARY KEY,
    from_iata         CHAR(3)      NOT NULL REFERENCES airports(iata),
    to_iata           CHAR(3)      NOT NULL REFERENCES airports(iata),
    from_city         TEXT,
    to_city           TEXT,
    distance_km       NUMERIC(8,1),
    avg_price_inr     INT,
    min_price_inr     INT,
    max_price_inr     INT,
    avg_price_per_km  NUMERIC(7,2),
    min_price_per_km  NUMERIC(7,2),
    cheapest_airline  TEXT,
    avg_co2_kg        INT,
    avg_co2_per_km    NUMERIC(6,2),
    avg_co2_vs_avg    NUMERIC(5,1),
    nonstop_available BOOLEAN,
    airline_count     SMALLINT,
    snapshot_count    INT          DEFAULT 0,
    last_updated      TIMESTAMPTZ  DEFAULT NOW()
);


-- ── 7. Scraper run log ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scraper_runs (
    id             SERIAL       PRIMARY KEY,
    run_at         TIMESTAMPTZ  DEFAULT NOW(),
    run_date       DATE         DEFAULT CURRENT_DATE,
    rows_written   INT          DEFAULT 0,
    routes_scraped INT          DEFAULT 0,
    duration_sec   NUMERIC(8,2),
    status         TEXT         DEFAULT 'ok',   -- 'ok' | 'partial' | 'failed'
    error_msg      TEXT
);


-- ── 8. Views ───────────────────────────────────────────────────

-- Latest price per route+airline+travel_date (most recent scrape wins)
CREATE OR REPLACE VIEW latest_prices AS
SELECT DISTINCT ON (route_code, airline, travel_date)
    id, route_code, from_iata, to_iata, from_city, to_city,
    airline, travel_date, scraped_at, days_before_travel,
    price_inr, departure_time, arrival_time,
    duration_minutes, stops, layover_airport,
    co2_kg, co2_vs_avg
FROM price_snapshots
ORDER BY route_code, airline, travel_date, scraped_at DESC;


-- Price curve: how price changes as travel date approaches
-- Core of your "will it get cheaper?" feature
CREATE OR REPLACE VIEW price_curves AS
SELECT
    route_code,
    airline,
    travel_date,
    days_before_travel,
    price_inr,
    scraped_at,
    ROUND(AVG(price_inr) OVER (
        PARTITION BY route_code, airline, days_before_travel
    )) AS avg_price_at_lead_time
FROM price_snapshots
ORDER BY route_code, airline, travel_date, days_before_travel DESC;


-- Airline head-to-head on same route
CREATE OR REPLACE VIEW airline_comparison AS
SELECT
    route_code,
    from_city,
    to_city,
    airline,
    COUNT(*)                      AS observations,
    ROUND(AVG(price_inr))         AS avg_price,
    MIN(price_inr)                AS min_price,
    ROUND(AVG(co2_vs_avg), 1)     AS avg_co2_vs_avg,
    ROUND(100 - AVG(co2_vs_avg))  AS eco_score
FROM price_snapshots
WHERE airline IS NOT NULL
GROUP BY route_code, from_city, to_city, airline;
