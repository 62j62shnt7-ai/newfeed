#!/usr/bin/env python3
"""
Movie RSS Tracker — scraper/enrichment job.

Runs inside GitHub Actions on a schedule. Loads the RSS feed through a real
headless browser once (to pass the site's bot check and harvest cookies),
then paginates the feed with a plain requests.Session using those cookies,
parses new items, enriches each with OMDb metadata, and merges the result
into data/movies.json — which the static site (index.html) reads directly.

No database, no local chromedriver management: Playwright manages its own
browser binary, so there's nothing to detect, download, or code-sign.
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

# =========================================================
# CONFIG
# =========================================================

FEED_URL = os.environ.get(
    "FEED_URL", "https://www.scnsrc.me/category/films/feed"
)
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")
OMDB_API_URL = "https://www.omdbapi.com/"

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "movies.json"

JUNK_WORDS = [
    "PROPER", "REPACK", "MULTI", "DUAL", "HDR", "HDR10", "DV", "ATMOS",
    "TRUEHD", "DDP5", "AAC", "BluRay", "BRRip", "WEBRip", "WEB", "NF",
    "AMZN", "HMAX", "1080p", "720p", "2160p", "x264", "x265", "h264", "h265",
]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# =========================================================
# HELPERS (ported as-is from the desktop app)
# =========================================================

def normalize_title(title):
    return re.sub(r"[^a-z0-9]", "", str(title).lower())


def build_feed_page_url(base_url, page):
    if page <= 1:
        return base_url.rstrip("/")
    return f"{base_url.rstrip('/')}/?paged={page}"


def clean_scene_title(raw_title):
    title = re.sub(r"[\._\-]+", " ", raw_title)
    title = re.sub(r"\[.*?\]", "", title)

    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    year = year_match.group(1) if year_match else None
    if year:
        title = title.split(year)[0]

    for junk in JUNK_WORDS:
        title = re.sub(rf"\b{junk}\b", "", title, flags=re.IGNORECASE)

    title = re.sub(r"\s+", " ", title).strip()
    return title, year


def extract_poster(desc):
    if not desc:
        return None
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
    return match.group(1) if match else None


# =========================================================
# OMDb
# =========================================================

def fetch_movie_metadata(title, year=None):
    try:
        query = title.strip()
        if not query:
            return None

        resp = requests.get(
            OMDB_API_URL,
            params={"apikey": OMDB_API_KEY, "s": query, "type": "movie"},
            timeout=20,
        )
        data = resp.json()

        if data.get("Response") != "True":
            return None

        results = data.get("Search", [])
        if not results:
            return None

        normalized_query = normalize_title(query)
        best_match, best_score = None, -1

        for result in results:
            candidate_title = result.get("Title", "")
            candidate_year = result.get("Year", "")
            normalized_candidate = normalize_title(candidate_title)

            score = 0
            if normalized_candidate == normalized_query:
                score += 100
            elif (
                normalized_query in normalized_candidate
                or normalized_candidate in normalized_query
            ):
                score += 50
            if year and str(year) in candidate_year:
                score += 25
            if result.get("Type") == "movie":
                score += 10

            if score > best_score:
                best_score, best_match = score, result

        if not best_match:
            best_match = results[0]

        imdb_id = best_match.get("imdbID")
        if not imdb_id:
            return None

        details_resp = requests.get(
            OMDB_API_URL,
            params={"apikey": OMDB_API_KEY, "i": imdb_id},
            timeout=20,
        )
        details = details_resp.json()

        if details.get("Response") != "True":
            return None

        return {
            "rating": details.get("imdbRating", "N/A"),
            "genre": details.get("Genre", "Unknown"),
            "runtime": details.get("Runtime", "Unknown"),
            "poster": details.get("Poster"),
            "imdb_id": details.get("imdbID"),
            "plot": details.get("Plot", ""),
        }

    except Exception as e:
        log(f"OMDb error for '{title}': {e}")
        return None


# =========================================================
# BROWSER SESSION (Playwright — installs/manages its own browser)
# =========================================================

def create_session(feed_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        log("Opening browser to pass bot check...")
        page.goto(feed_url, timeout=60000)
        time.sleep(12)  # let any JS challenge resolve

        cookies = context.cookies()
        user_agent = page.evaluate("() => navigator.userAgent")

        browser.close()

    session = requests.Session()
    for cookie in cookies:
        session.cookies.set(cookie["name"], cookie["value"])
    session.headers.update({"User-Agent": user_agent})

    log(f"Cookies harvested: {len(cookies)}")
    return session


def fetch_feed(session, base_url, page_number):
    url = build_feed_page_url(base_url, page_number)
    try:
        log(f"Fetching {url}")
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            log(f"HTTP {resp.status_code}")
            return None
        payload = resp.content
        if b"<rss" not in payload.lower():
            log("Invalid RSS payload")
            return None
        return payload
    except Exception as e:
        log(f"Feed error: {e}")
        return None


# =========================================================
# MERGE INTO data/movies.json
# =========================================================

def load_existing():
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text())
        except Exception:
            pass
    return {"last_updated": None, "movies": [], "watchlist": [], "excluded": []}

def save(store):
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    store["last_updated"] = datetime.now(timezone.utc).isoformat()
    # If the database store doesn't have these arrays initialization parameters, preserve them empty
    if "watchlist" not in store:
        store["watchlist"] = []
    if "excluded" not in store:
        store["excluded"] = []
    DATA_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False))


def process_feed(payload, store, seen_keys):
    added = 0
    try:
        payload = payload.lstrip(b"\xef\xbb\xbf").lstrip()
        root = ET.fromstring(payload)
    except Exception as e:
        log(f"XML parse error: {e}")
        return 0

    channel = root.find("channel")
    if channel is None:
        return 0

    items = channel.findall("item")
    log(f"Found {len(items)} items on this page")

    for item in items:
        title_node = item.find("title")
        guid_node = item.find("guid")
        desc_node = item.find("description")

        raw_title = (
            title_node.text.strip()
            if title_node is not None and title_node.text
            else ""
        )
        if not raw_title:
            continue

        clean_title, year = clean_scene_title(raw_title)
        slug = normalize_title(clean_title)
        key = (slug, year)

        if key in seen_keys:
            continue

        metadata = fetch_movie_metadata(clean_title, year)
        poster = extract_poster(desc_node.text if desc_node is not None else "")

        rating, genre, runtime, imdb_id, plot = "N/A", "Unknown", "Unknown", None, ""
        if metadata:
            rating = metadata.get("rating", "N/A")
            genre = metadata.get("genre", "Unknown")
            runtime = metadata.get("runtime", "Unknown")
            imdb_id = metadata.get("imdb_id")
            plot = metadata.get("plot", "")
            if not poster:
                poster = metadata.get("poster")

        store["movies"].append({
            "guid": guid_node.text if guid_node is not None else raw_title,
            "slug": slug,
            "title": clean_title,
            "year": year,
            "rating": rating,
            "poster": poster if poster and poster != "N/A" else None,
            "genre": genre,
            "runtime": runtime,
            "imdb_id": imdb_id,
            "plot": plot,
            "date_added": datetime.now(timezone.utc).isoformat(),
        })

        seen_keys.add(key)
        added += 1
        log(f"Added: {clean_title} ({year}) | IMDb {rating}")

        time.sleep(0.3)  # be polite to OMDb

    return added


def main():
    if not OMDB_API_KEY:
        log("WARNING: OMDB_API_KEY not set — metadata will be skipped.")

    store = load_existing()
    seen_keys = {(m["slug"], m["year"]) for m in store["movies"]}

    session = create_session(FEED_URL)
    if not session:
        log("Could not establish a session — aborting.")
        sys.exit(1)

    total_added = 0
    for page in range(1, MAX_PAGES + 1):
        payload = fetch_feed(session, FEED_URL, page)
        if not payload:
            continue
        total_added += process_feed(payload, store, seen_keys)
        time.sleep(1)

    save(store)
    log(f"Done. Added {total_added} new movie(s). Total: {len(store['movies'])}")


if __name__ == "__main__":
    main()
