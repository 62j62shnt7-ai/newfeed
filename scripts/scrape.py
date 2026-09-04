#!/usr/bin/env python3
"""
Movie RSS Tracker — scraper/enrichment job.

Runs inside GitHub Actions on a schedule. Loads the RSS feed through a real
headless browser once (to pass the site's bot check and harvest cookies),
then paginates the feed with a plain requests.Session using those cookies,
parses new items, enriches each with TMDb metadata, and merges the result
into data/movies.json — which the static site (index.html) reads directly.

No database, no local chromedriver management: Playwright manages its own
browser binary, so there's nothing to detect, download, or code-sign.
"""

import argparse
import gzip
import io
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
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_API_URL = "https://api.themoviedb.org/3"
IMDB_RATINGS_DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "movies.json"

JUNK_WORDS = [
    "PROPER", "REPACK", "MULTI", "DUAL", "HDR", "HDR10", "DV", "ATMOS",
    "TRUEHD", "DDP5", "AAC", "BluRay", "BRRip", "WEBRip", "WEB", "WEB-DL",
    "WEBDL", "DL", "NF", "AMZN", "HMAX", "1080p", "720p", "2160p", "x264",
    "x265", "h264", "h265", "EXTENDED", "REMASTERED", "UNRATED", "DIRECTORS CUT",
    "DIRECTOR'S CUT", "IMAX", "HYBRID", "CRITERION", "REMUX", "LIMITED",
    "COMPLETE", "SUBBED", "DUBBED", "INTERNAL", "READNFO", "FESTIVAL",
    "DOCU", "ATVP", "DSNP", "PARAMOUNT", "PEACOCK", "HULU", "UHD", "FLAC",
    "AV1", "HEVC", "DTS", "DTS-HD",
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
    # Pre-strip trailing release group before punctuation stripping
    working = re.sub(r"-[A-Za-z0-9]+$", "", raw_title.strip())
    title = re.sub(r"[\._\-]+", " ", working)
    title = re.sub(r"\[.*?\]", "", title)

    year_matches = list(re.finditer(r"\b(19\d{2}|20\d{2})\b", title))
    year = None
    if year_matches:
        selected_match = None
        for m in reversed(year_matches):
            candidate_prefix = title[:m.start()].strip()
            test_prefix = candidate_prefix
            for junk in JUNK_WORDS:
                test_prefix = re.sub(rf"\b{re.escape(junk)}\b", "", test_prefix, flags=re.IGNORECASE)
            if test_prefix.strip():
                selected_match = m
                break

        if selected_match:
            year = selected_match.group(1)
            title = title[:selected_match.start()]
        elif len(year_matches) == 1 and not title[:year_matches[0].start()].strip():
            year = None

    for junk in JUNK_WORDS:
        title = re.sub(rf"\b{re.escape(junk)}\b", "", title, flags=re.IGNORECASE)

    # Strip AKA / a.k.a. alternative titles to prevent failed TMDb queries
    aka_split = re.split(r"\s+\b(?:aka|a\.k\.a\.)\b\s+", title, flags=re.IGNORECASE)
    if len(aka_split) > 1 and aka_split[0].strip():
        title = aka_split[0].strip()

    title = re.sub(r"\s+", " ", title).strip()
    return title, year


def extract_poster(desc):
    if not desc:
        return None
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
    return match.group(1) if match else None


# =========================================================
# TMDb
# =========================================================

def _tmdb_request(path, params=None):
    if not TMDB_API_KEY:
        return None
    url = f"{TMDB_API_URL}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    req_params = dict(params or {})
    if TMDB_API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        req_params["api_key"] = TMDB_API_KEY
    resp = requests.get(url, params=req_params, headers=headers, timeout=20)
    if resp.status_code != 200:
        return None
    return resp.json()


def fetch_movie_metadata(title, year=None):
    try:
        query = title.strip()
        if not query or not TMDB_API_KEY:
            return None

        req_year_str = str(year).strip() if year else None

        # 1. Search TMDb using year constraint if available
        results = []
        if req_year_str and req_year_str.isdigit():
            try:
                data = _tmdb_request("search/movie", {"query": query, "year": req_year_str, "include_adult": "false"})
                if data and data.get("results"):
                    results = data.get("results", [])
            except Exception as e:
                log(f"TMDb search error for '{title}' (year={req_year_str}): {e}")

        # 2. Fallback search without year constraint if year search yielded nothing
        if not results:
            try:
                data = _tmdb_request("search/movie", {"query": query, "include_adult": "false"})
                if data and data.get("results"):
                    results = data.get("results", [])
            except Exception as e:
                log(f"TMDb fallback search error for '{title}': {e}")

        if not results:
            return None

        # 3. Score candidates
        normalized_query = normalize_title(query)
        best_match, best_score = None, -1

        for result in results:
            candidate_title = result.get("title", "")
            release_date = result.get("release_date", "")
            candidate_year_raw = release_date[:4] if release_date else ""
            normalized_candidate = normalize_title(candidate_title)

            score = 0
            if normalized_candidate == normalized_query:
                score += 100
            elif (
                normalized_query in normalized_candidate
                or normalized_candidate in normalized_query
            ):
                score += 50

            # Year bonus / penalty
            if req_year_str:
                if req_year_str == candidate_year_raw:
                    score += 30
                elif candidate_year_raw and req_year_str.isdigit() and candidate_year_raw.isdigit() and abs(int(req_year_str) - int(candidate_year_raw)) <= 1:
                    score += 15
                elif candidate_year_raw:
                    # Penalty for distant year mismatch
                    score -= 40

            # Popularity / vote count bonus as subtle tie-breaker
            popularity = result.get("popularity", 0)
            if popularity:
                score += min(10, float(popularity) / 10.0)

            if score > best_score and score > 0:
                best_score, best_match = score, result

        if not best_match:
            return None

        tmdb_id = best_match.get("id")
        if not tmdb_id:
            return None

        details = _tmdb_request(f"movie/{tmdb_id}", {"append_to_response": "external_ids,videos,credits"})
        if not details:
            return None

        vote_avg = details.get("vote_average")
        vote_cnt = details.get("vote_count", 0)
        rating_str = f"{vote_avg:.1f}" if (vote_avg is not None and vote_cnt > 0 and vote_avg > 0) else "N/A"

        genres_list = [g.get("name") for g in details.get("genres", []) if g.get("name")]
        genre_str = ", ".join(genres_list) if genres_list else "Unknown"

        runtime_min = details.get("runtime")
        runtime_str = f"{runtime_min} min" if runtime_min else "Unknown"

        poster_path = details.get("poster_path")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

        backdrop_path = details.get("backdrop_path")
        backdrop_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else None

        ext_ids = details.get("external_ids") or {}
        imdb_id = ext_ids.get("imdb_id") or details.get("imdb_id")

        plot_str = details.get("overview") or ""

        # Extract YouTube trailer
        trailer_key = None
        videos = (details.get("videos") or {}).get("results", [])
        for v in videos:
            if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser"):
                trailer_key = v.get("key")
                if v.get("type") == "Trailer":
                    break

        # Extract director(s)
        crew = (details.get("credits") or {}).get("crew", [])
        directors = [c.get("name") for c in crew if c.get("job") == "Director" and c.get("name")]
        director_str = ", ".join(directors[:2]) if directors else None

        # Extract top cast
        cast_list = (details.get("credits") or {}).get("cast", [])
        cast_str = ", ".join([c.get("name") for c in cast_list[:4] if c.get("name")]) if cast_list else None

        return {
            "rating": rating_str,
            "genre": genre_str,
            "runtime": runtime_str,
            "poster": poster_url,
            "backdrop": backdrop_url,
            "imdb_id": imdb_id,
            "plot": plot_str,
            "trailer_key": trailer_key,
            "director": director_str,
            "cast": cast_str,
        }

    except Exception as e:
        log(f"TMDb error for '{title}': {e}")
        return None


# =========================================================
# OFFICIAL IMDB DAILY DATASET ENRICHMENT
# =========================================================

def fetch_official_imdb_ratings(imdb_ids):
    """
    Downloads IMDb's official daily title.ratings.tsv.gz export and extracts
    ratings for the given set of imdb_ids (e.g. {'tt1234567', ...}).
    """
    valid_ids = {i for i in imdb_ids if i and str(i).startswith("tt")}
    if not valid_ids:
        return {}

    log(f"Downloading official IMDb daily ratings dataset ({len(valid_ids)} target IDs)...")
    ratings_map = {}
    try:
        resp = requests.get(
            IMDB_RATINGS_DATASET_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=60,
            stream=True
        )
        if resp.status_code != 200:
            log(f"IMDb dataset HTTP status {resp.status_code}")
            return {}

        resp.raw.decode_content = False
        with gzip.GzipFile(fileobj=resp.raw) as gz:
            for line_bytes in gz:
                line = line_bytes.decode("utf-8", errors="ignore").rstrip("\r\n")
                parts = line.split("\t")
                if len(parts) >= 2:
                    tconst, avg_rating = parts[0], parts[1]
                    if tconst in valid_ids:
                        ratings_map[tconst] = avg_rating
                        if len(ratings_map) >= len(valid_ids):
                            break

        log(f"Matched {len(ratings_map)} official IMDb rating(s) from daily dataset.")
    except Exception as e:
        log(f"Error reading IMDb ratings dataset: {e}")

    return ratings_map


def sync_official_imdb_ratings(store):
    """
    Enriches all non-excluded movies with official IMDb ratings from title.ratings.tsv.gz.
    """
    excluded_set = set(store.get("excluded", []))
    imdb_ids = {
        m.get("imdb_id") for m in store.get("movies", [])
        if m.get("imdb_id") and m.get("guid") not in excluded_set and m.get("slug") not in excluded_set
    }
    if not imdb_ids:
        return

    official_ratings = fetch_official_imdb_ratings(imdb_ids)
    if not official_ratings:
        return

    updated_count = 0
    for m in store.get("movies", []):
        imdb_id = m.get("imdb_id")
        if imdb_id and imdb_id in official_ratings:
            new_rating = official_ratings[imdb_id]
            if new_rating and new_rating != "N/A" and m.get("rating") != new_rating:
                m["rating"] = new_rating
                updated_count += 1

    if updated_count > 0:
        log(f"Synced official IMDb ratings for {updated_count} movie(s).")


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
            store = json.loads(DATA_PATH.read_text())
            if not isinstance(store, dict):
                store = {}
            store.setdefault("movies", [])
            store.setdefault("watchlist", [])
            store.setdefault("excluded", [])
            return store
        except Exception as e:
            log(f"Error reading existing data/movies.json: {e}")
    return {"last_updated": None, "movies": [], "watchlist": [], "excluded": []}

def save(store):
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    store["last_updated"] = datetime.now(timezone.utc).isoformat()
    if "watchlist" not in store or not isinstance(store["watchlist"], list):
        store["watchlist"] = []
    if "excluded" not in store or not isinstance(store["excluded"], list):
        store["excluded"] = []
    if "movies" not in store or not isinstance(store["movies"], list):
        store["movies"] = []
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
        backdrop, trailer_key, director, cast = None, None, None, None
        if metadata:
            rating = metadata.get("rating", "N/A")
            genre = metadata.get("genre", "Unknown")
            runtime = metadata.get("runtime", "Unknown")
            imdb_id = metadata.get("imdb_id")
            plot = metadata.get("plot", "")
            backdrop = metadata.get("backdrop")
            trailer_key = metadata.get("trailer_key")
            director = metadata.get("director")
            cast = metadata.get("cast")
            if not poster:
                poster = metadata.get("poster")

        guid_val = guid_node.text.strip() if guid_node is not None and guid_node.text else raw_title
        source_url = guid_val if guid_val.startswith("http") else None

        store["movies"].append({
            "guid": guid_val,
            "source_url": source_url,
            "raw_title": raw_title,
            "slug": slug,
            "title": clean_title,
            "year": year,
            "rating": rating,
            "poster": poster if poster and poster != "N/A" else None,
            "backdrop": backdrop,
            "trailer_key": trailer_key,
            "director": director,
            "cast": cast,
            "genre": genre,
            "runtime": runtime,
            "imdb_id": imdb_id,
            "plot": plot,
            "date_added": datetime.now(timezone.utc).isoformat(),
        })

        seen_keys.add(key)
        added += 1
        log(f"Added: {clean_title} ({year}) | IMDb {rating}")

        time.sleep(0.2)  # be polite to TMDb

    return added


def main():
    parser = argparse.ArgumentParser(description="Movie RSS Tracker Scraper")
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="Re-fetch TMDb metadata for ALL existing movies in data/movies.json",
    )
    parser.add_argument(
        "--refetch-unrated",
        dest="refetch_unrated",
        action="store_true",
        default=True,
        help="Re-fetch TMDb metadata for unrated or incomplete movies (default: True)",
    )
    parser.add_argument(
        "--no-refetch-unrated",
        dest="refetch_unrated",
        action="store_false",
        help="Disable automatic re-fetching of unrated/incomplete movies",
    )
    args = parser.parse_args()

    refetch_all = args.refetch or os.environ.get("REFETCH", "").lower() in ("true", "1", "yes")
    env_unrated = os.environ.get("REFETCH_UNRATED", "").lower()
    refetch_unrated = args.refetch_unrated if not env_unrated else env_unrated in ("true", "1", "yes")

    if not TMDB_API_KEY:
        log("WARNING: TMDB_API_KEY not set — metadata will be skipped.")

    store = load_existing()
    excluded_set = set(store.get("excluded", []))

    if refetch_all:
        target_movies = [
            m for m in store["movies"]
            if m.get("guid") not in excluded_set and m.get("slug") not in excluded_set
        ]
        log(f"Full refetch mode enabled — re-evaluating TMDb metadata for {len(target_movies)} non-excluded movie(s)...")
        updated_count = 0
        for i, m in enumerate(target_movies):
            clean_title = m.get("title", "")
            year = m.get("year")
            if not clean_title:
                continue
            log(f"[{i+1}/{len(target_movies)}] Refetching: {clean_title} ({year})...")
            metadata = fetch_movie_metadata(clean_title, year)
            if metadata:
                m["rating"] = metadata.get("rating", "N/A")
                m["genre"] = metadata.get("genre", "Unknown")
                m["runtime"] = metadata.get("runtime", "Unknown")
                m["imdb_id"] = metadata.get("imdb_id")
                m["plot"] = metadata.get("plot", "")
                if metadata.get("poster") and metadata.get("poster") != "N/A":
                    m["poster"] = metadata.get("poster")
                if metadata.get("backdrop"):
                    m["backdrop"] = metadata.get("backdrop")
                if metadata.get("trailer_key"):
                    m["trailer_key"] = metadata.get("trailer_key")
                if metadata.get("director"):
                    m["director"] = metadata.get("director")
                if metadata.get("cast"):
                    m["cast"] = metadata.get("cast")
                updated_count += 1
                log(f"  -> Updated: {clean_title} ({year}) | Rating {m['rating']} | IMDb ID: {m['imdb_id']}")
            else:
                log(f"  -> No valid TMDb match found for {clean_title} ({year}).")
            time.sleep(0.2)
        sync_official_imdb_ratings(store)
        save(store)
        log(f"Full refetch complete. Re-evaluated {updated_count}/{len(target_movies)} movies.")
    elif refetch_unrated and TMDB_API_KEY:
        unrated_movies = [
            m for m in store["movies"]
            if (m.get("rating") in ("N/A", None, "", "0.0") or not m.get("imdb_id"))
            and m.get("guid") not in excluded_set
            and m.get("slug") not in excluded_set
        ]
        if unrated_movies:
            log(f"Unrated/Incomplete refetch enabled — checking TMDb for {len(unrated_movies)} non-excluded movie(s)...")
            updated_count = 0
            for i, m in enumerate(unrated_movies):
                clean_title = m.get("title", "")
                year = m.get("year")
                if not clean_title:
                    continue
                log(f"[{i+1}/{len(unrated_movies)}] Refetching unrated: {clean_title} ({year})...")
                metadata = fetch_movie_metadata(clean_title, year)
                if metadata:
                    old_rating = m.get("rating", "N/A")
                    m["rating"] = metadata.get("rating", "N/A")
                    m["genre"] = metadata.get("genre", "Unknown")
                    m["runtime"] = metadata.get("runtime", "Unknown")
                    m["imdb_id"] = metadata.get("imdb_id")
                    m["plot"] = metadata.get("plot", "")
                    if metadata.get("poster") and metadata.get("poster") != "N/A":
                        m["poster"] = metadata.get("poster")
                    if metadata.get("backdrop"):
                        m["backdrop"] = metadata.get("backdrop")
                    if metadata.get("trailer_key"):
                        m["trailer_key"] = metadata.get("trailer_key")
                    if metadata.get("director"):
                        m["director"] = metadata.get("director")
                    if metadata.get("cast"):
                        m["cast"] = metadata.get("cast")
                    updated_count += 1
                    log(f"  -> Refetched: {clean_title} ({year}) | Rating: {old_rating} -> {m['rating']} | IMDb ID: {m['imdb_id']}")
                else:
                    log(f"  -> Still no TMDb match for {clean_title} ({year}).")
                time.sleep(0.2)
            sync_official_imdb_ratings(store)
            save(store)
            log(f"Unrated refetch complete. Updated {updated_count}/{len(unrated_movies)} movie(s).")

    seen_keys = {(m.get("slug", ""), m.get("year")) for m in store["movies"]}

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

    sync_official_imdb_ratings(store)
    save(store)
    log(f"Done. Added {total_added} new movie(s). Total: {len(store['movies'])}")


if __name__ == "__main__":
    main()
