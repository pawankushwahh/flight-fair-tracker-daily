-- ============================================================
-- Supabase RPC: refresh_route_summary()
-- Run this in Supabase SQL Editor AFTER schema.sql
-- Called daily by daily_scraper.py via sb.rpc(...)
-- ============================================================

CREATE OR REPLACE FUNCTION refresh_route_summary()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    -- FIX: Changed from DELETE+INSERT to UPSERT (ON CONFLICT DO UPDATE).
    --      DELETE+INSERT caused a brief window where route_summary was empty,
    --      which would return blank results to anyone hitting the API mid-refresh.
    --      UPSERT updates rows in-place — no downtime.
    --
    -- FIX: Distance now comes from route_distances (a geometric fact table),
    --      not from route_airlines (a config table). Route_airlines didn't
    --      always have distance set, causing NULL price_per_km silently.

    INSERT INTO route_summary (
        route_code, from_iata, to_iata, from_city, to_city,
        distance_km,
        avg_price_inr, min_price_inr, max_price_inr,
        avg_price_per_km, min_price_per_km,
        cheapest_airline,
        avg_co2_kg, avg_co2_per_km, avg_co2_vs_avg,
        nonstop_available, airline_count,
        snapshot_count, last_updated
    )
    SELECT
        ps.route_code,
        ps.from_iata,
        ps.to_iata,
        ps.from_city,
        ps.to_city,
        rd.distance_km,

        ROUND(AVG(ps.price_inr))::INT                            AS avg_price_inr,
        MIN(ps.price_inr)                                        AS min_price_inr,
        MAX(ps.price_inr)                                        AS max_price_inr,

        ROUND(AVG(ps.price_inr) / NULLIF(rd.distance_km, 0), 2) AS avg_price_per_km,
        ROUND(MIN(ps.price_inr) / NULLIF(rd.distance_km, 0), 2) AS min_price_per_km,

        -- Cheapest airline by average price on this route
        (
            SELECT p2.airline
            FROM price_snapshots p2
            WHERE p2.route_code = ps.route_code
              AND p2.airline IS NOT NULL
              AND p2.price_inr IS NOT NULL
            GROUP BY p2.airline
            ORDER BY AVG(p2.price_inr)
            LIMIT 1
        )                                                        AS cheapest_airline,

        ROUND(AVG(ps.co2_kg))::INT                              AS avg_co2_kg,
        ROUND(AVG(ps.co2_kg * 1000.0
              / NULLIF(rd.distance_km, 0)), 2)                  AS avg_co2_per_km,
        ROUND(AVG(ps.co2_vs_avg), 1)                            AS avg_co2_vs_avg,

        BOOL_OR(ps.stops = 0)                                   AS nonstop_available,
        COUNT(DISTINCT ps.airline)::SMALLINT                    AS airline_count,
        COUNT(*)::INT                                           AS snapshot_count,
        NOW()                                                   AS last_updated

    FROM price_snapshots ps
    -- FIX: JOIN route_distances (not route_airlines) for distance
    JOIN route_distances rd ON rd.route_code = ps.route_code
    WHERE ps.price_inr IS NOT NULL
    GROUP BY ps.route_code, ps.from_iata, ps.to_iata,
             ps.from_city, ps.to_city, rd.distance_km
    HAVING COUNT(*) >= 2

    ON CONFLICT (route_code) DO UPDATE SET
        avg_price_inr     = EXCLUDED.avg_price_inr,
        min_price_inr     = EXCLUDED.min_price_inr,
        max_price_inr     = EXCLUDED.max_price_inr,
        avg_price_per_km  = EXCLUDED.avg_price_per_km,
        min_price_per_km  = EXCLUDED.min_price_per_km,
        cheapest_airline  = EXCLUDED.cheapest_airline,
        avg_co2_kg        = EXCLUDED.avg_co2_kg,
        avg_co2_per_km    = EXCLUDED.avg_co2_per_km,
        avg_co2_vs_avg    = EXCLUDED.avg_co2_vs_avg,
        nonstop_available = EXCLUDED.nonstop_available,
        airline_count     = EXCLUDED.airline_count,
        snapshot_count    = EXCLUDED.snapshot_count,
        last_updated      = NOW();
END;
$$;


-- ============================================================
-- Useful analysis queries — run manually in Supabase SQL Editor
-- ============================================================

-- Price curve for a route × airline (how price moves over time)
SELECT
    travel_date,
    days_before_travel,
    airline,
    AVG(price_inr) AS avg_price,
    MIN(price_inr) AS min_price
FROM price_snapshots
WHERE route_code = 'DEL-BOM'
  AND airline ILIKE '%indigo%'
GROUP BY travel_date, days_before_travel, airline
ORDER BY days_before_travel DESC;


-- Cheapest route per ₹/km
SELECT route_code, avg_price_inr, distance_km, avg_price_per_km, cheapest_airline
FROM route_summary
ORDER BY avg_price_per_km ASC
LIMIT 20;


-- Best day of week to fly (needs 30+ days of data)
SELECT
    TO_CHAR(travel_date, 'Day')   AS weekday,
    EXTRACT(DOW FROM travel_date) AS dow_num,
    ROUND(AVG(price_inr))         AS avg_price,
    COUNT(*)                      AS samples
FROM price_snapshots
WHERE route_code = 'DEL-BOM'
GROUP BY weekday, dow_num
ORDER BY dow_num;


-- CO2 efficiency by airline (needs co2_vs_avg populated)
SELECT
    airline,
    ROUND(AVG(co2_kg))        AS avg_co2_kg,
    ROUND(AVG(co2_vs_avg), 1) AS avg_vs_baseline,
    ROUND(100 - AVG(co2_vs_avg)) AS eco_score,
    COUNT(*)                  AS samples
FROM price_snapshots
WHERE co2_kg IS NOT NULL
GROUP BY airline
ORDER BY eco_score DESC;


-- Health check: which days had issues
SELECT run_date, rows_written, routes_scraped, status, error_msg
FROM scraper_runs
ORDER BY run_date DESC
LIMIT 30;


-- Price spike alert: routes >40% above their historical average
-- (useful once you have 30+ days of data)
WITH baseline AS (
    SELECT route_code, airline,
           AVG(price_inr) AS hist_avg
    FROM price_snapshots
    WHERE days_before_travel BETWEEN 80 AND 90
    GROUP BY route_code, airline
),
current_price AS (
    SELECT DISTINCT ON (route_code, airline)
           route_code, airline, price_inr, travel_date
    FROM price_snapshots
    ORDER BY route_code, airline, travel_date, scraped_at DESC
)
SELECT
    c.route_code,
    c.airline,
    c.travel_date,
    c.price_inr                                                      AS current_price,
    ROUND(b.hist_avg)::INT                                           AS hist_avg_price,
    ROUND((c.price_inr - b.hist_avg) / b.hist_avg * 100)            AS pct_above_avg
FROM current_price c
JOIN baseline b USING (route_code, airline)
WHERE c.price_inr > b.hist_avg * 1.4
ORDER BY pct_above_avg DESC;
