"""
Seed a Chrome profile with synthetic realtor.com browsing history.

Writing fake-but-plausible visit records into Chrome's History SQLite DB before
the first browser launch makes a brand-new profile look like a returning user
rather than a freshly minted bot session. PerimeterX and KPSDK score on session
age, cookie presence, and navigation continuity — a profile with zero history is
a near-perfect bot signal.

Must be called BEFORE Chrome is launched against the profile directory.
Chrome holds an exclusive lock on its SQLite files while running.

Usage
-----
    from re_analyzer.scrapers.realtor_profile_seeder import seed_chrome_profile

    n = seed_chrome_profile("/path/to/user-data-dir", profile_subdir="Default")
    # Returns number of visit records inserted (0 = already seeded or skipped).
"""
from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path
from typing import List, Tuple

# Chrome stores timestamps as microseconds since 1601-01-01 00:00:00 UTC.
# Unix epoch (1970-01-01) is 11 644 473 600 seconds after that anchor.
_CHROME_EPOCH_OFFSET_S = 11_644_473_600

def _to_chrome_us(unix_ts: float) -> int:
    return int((unix_ts + _CHROME_EPOCH_OFFSET_S) * 1_000_000)


# Page transition codes — same values Chrome writes into the visits table.
_TRANSITION_TYPED = 0x02000001   # user typed URL directly into address bar
_TRANSITION_LINK  = 0x00000000   # user clicked a link

# ---------------------------------------------------------------------------
# URL pool
# ---------------------------------------------------------------------------

_CONTENT_URLS: List[Tuple[str, str]] = [
    ("https://www.realtor.com/", "Real Estate Listings & Homes For Sale | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Florida", "Florida Real Estate – Homes For Sale | realtor.com®"),
    ("https://www.realtor.com/real-estate/Florida/", "Florida Homes for Sale and Real Estate | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Miami_FL", "Miami FL Real Estate & Homes For Sale | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Tampa_FL", "Tampa FL Real Estate & Homes For Sale | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Orlando_FL", "Orlando FL Real Estate & Homes For Sale | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Jacksonville_FL", "Jacksonville FL Real Estate | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/St-Petersburg_FL", "St. Petersburg FL Real Estate | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Fort-Lauderdale_FL", "Fort Lauderdale FL Real Estate | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Clearwater_FL", "Clearwater FL Real Estate | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Gainesville_FL", "Gainesville FL Real Estate | realtor.com®"),
    ("https://www.realtor.com/realestateandhomes-search/Tallahassee_FL", "Tallahassee FL Real Estate | realtor.com®"),
    ("https://www.realtor.com/local/", "Find a Realtor® or Real Estate Agent | realtor.com®"),
    ("https://www.realtor.com/research/florida-housing-market/", "Florida Housing Market: Prices, Trends & Forecast | realtor.com®"),
    ("https://www.realtor.com/research/housing-market/", "Housing Market Trends & Reports | realtor.com®"),
    ("https://www.realtor.com/mortgage/", "Mortgage Rates & Calculator | realtor.com®"),
    ("https://www.realtor.com/news/trends/", "Real Estate News & Trends | realtor.com®"),
    ("https://www.realtor.com/advice/buy/", "Home Buying Tips & Advice | realtor.com®"),
    ("https://www.realtor.com/advice/buy/how-to-make-an-offer-on-a-house/", "How to Make an Offer on a House | realtor.com®"),
    ("https://www.realtor.com/advice/finance/how-much-house-can-i-afford/", "How Much House Can I Afford? | realtor.com®"),
]

# Realistic-looking property detail URLs (fake IDs, plausible structure).
_PROPERTY_URLS: List[Tuple[str, str]] = [
    (
        f"https://www.realtor.com/realestateandhomes-detail/"
        f"{addr}_FL_{zip_}_{pid}",
        f"{street}, {city}, FL {zip_} | realtor.com®",
    )
    for addr, street, city, zip_, pid in [
        ("123-Oak-Ave_Miami", "123 Oak Ave", "Miami", "33101", "M8823471290"),
        ("456-Palm-Dr_Tampa", "456 Palm Dr", "Tampa", "33601", "M7712384561"),
        ("789-Bay-Blvd_Orlando", "789 Bay Blvd", "Orlando", "32801", "M6601293872"),
        ("321-Sunset-Ln_Jacksonville", "321 Sunset Ln", "Jacksonville", "32201", "M5590182963"),
        ("654-River-Rd_St-Petersburg", "654 River Rd", "St. Petersburg", "33701", "M4489071054"),
    ]
]

_ALL_URLS = _CONTENT_URLS + _PROPERTY_URLS


def seed_chrome_profile(
    profile_dir: str,
    *,
    profile_subdir: str = "Default",
    weeks_back: float = 7.0,
    n_visits: int = 90,
    overwrite: bool = False,
) -> int:
    """
    Plant synthetic realtor.com browsing history into a Chrome profile.

    Parameters
    ----------
    profile_dir     Chrome ``--user-data-dir`` path.
    profile_subdir  The profile directory inside user-data-dir (usually "Default").
    weeks_back      How far back to spread the synthetic visit timestamps.
    n_visits        Approximate number of visit records to insert.
    overwrite       If False (default), skip silently when History already exists.

    Returns
    -------
    Number of visit records inserted.  Returns 0 when skipped.
    """
    profile_path = Path(profile_dir).expanduser().resolve() / profile_subdir
    profile_path.mkdir(parents=True, exist_ok=True)
    history_path = profile_path / "History"

    if history_path.exists() and not overwrite:
        return 0

    visits_by_url: dict = {}
    now = time.time()
    earliest = now - weeks_back * 7 * 86_400

    for _ in range(n_visits):
        # Bias timestamps toward weekday evenings (7 pm – 10 pm) and weekend afternoons.
        raw_ts = random.uniform(earliest, now - 7_200)  # at least 2 h ago
        if random.random() < 0.65:
            day_start = int(raw_ts / 86_400) * 86_400
            hour_offset = random.uniform(19, 22) * 3_600 + random.uniform(-3_600, 3_600)
            raw_ts = day_start + hour_offset

        url, title = random.choice(_ALL_URLS)
        duration_us = int(random.uniform(20, 320) * 1_000_000)
        typed = random.random() < 0.12  # ~12 % typed directly

        if url not in visits_by_url:
            visits_by_url[url] = {"title": title, "timestamps": [], "typed_count": 0}
        visits_by_url[url]["timestamps"].append((raw_ts, duration_us, typed))
        if typed:
            visits_by_url[url]["typed_count"] += 1

    _write_history_db(history_path, visits_by_url)
    return n_visits


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _write_history_db(history_path: Path, visits_by_url: dict) -> None:
    conn = sqlite3.connect(str(history_path))
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        for url, data in visits_by_url.items():
            timestamps = sorted(data["timestamps"], key=lambda x: x[0])
            last_visit = _to_chrome_us(timestamps[-1][0])
            cur.execute(
                "INSERT OR IGNORE INTO urls "
                "(url, title, visit_count, typed_count, last_visit_time, hidden) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (url, data["title"], len(timestamps), data["typed_count"], last_visit),
            )
            row = cur.execute("SELECT id FROM urls WHERE url = ?", (url,)).fetchone()
            if not row:
                continue
            url_id = row[0]
            prev_id = 0
            for unix_ts, duration_us, typed in timestamps:
                transition = _TRANSITION_TYPED if typed else _TRANSITION_LINK
                cur.execute(
                    "INSERT INTO visits "
                    "(url, visit_time, from_visit, transition, visit_duration, "
                    "increments_omnibox_typed_score) VALUES (?, ?, ?, ?, ?, ?)",
                    (url_id, _to_chrome_us(unix_ts), prev_id, transition, duration_us, typed),
                )
                prev_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key   LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY,
            value LONGVARCHAR
        );
        CREATE TABLE IF NOT EXISTS urls (
            id              INTEGER PRIMARY KEY,
            url             LONGVARCHAR NOT NULL,
            title           LONGVARCHAR DEFAULT '',
            visit_count     INTEGER DEFAULT 0 NOT NULL,
            typed_count     INTEGER DEFAULT 0 NOT NULL,
            last_visit_time INTEGER NOT NULL,
            hidden          INTEGER DEFAULT 0 NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visits (
            id                              INTEGER PRIMARY KEY,
            url                             INTEGER NOT NULL,
            visit_time                      INTEGER NOT NULL,
            from_visit                      INTEGER DEFAULT 0,
            transition                      INTEGER DEFAULT 0 NOT NULL,
            segment_id                      INTEGER DEFAULT 0,
            visit_duration                  INTEGER DEFAULT 0 NOT NULL,
            increments_omnibox_typed_score  BOOLEAN DEFAULT FALSE NOT NULL,
            opener_visit                    INTEGER DEFAULT 0,
            originator_cache_guid           TEXT NOT NULL DEFAULT '',
            originator_visit_id             INTEGER DEFAULT 0,
            consider_for_ntp_most_visited   BOOLEAN NOT NULL DEFAULT FALSE,
            publicly_routable               BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE INDEX IF NOT EXISTS visits_url_index  ON visits (url);
        CREATE INDEX IF NOT EXISTS visits_time_index ON visits (visit_time);
        INSERT OR IGNORE INTO meta (key, value) VALUES ('version', '64');
        INSERT OR IGNORE INTO meta (key, value) VALUES ('last_compatible_version', '16');
    """)
