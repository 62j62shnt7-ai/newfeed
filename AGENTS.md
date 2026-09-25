# Scene Reel (NewFeed) — Agent Guidelines & Architecture

This document provides architecture specifications, directory maps, and operational guidelines for AI agents working in this repository to prevent regressions and keep token usage low.

---

## 1. Project Overview & Tech Stack
* **Frontend Web App:** Static single-page application (`index.html`) using Vanilla HTML5, CSS3, and ES6+ JavaScript. Deployed directly via **GitHub Pages** (zero build step).
* **Automation / Scraper:** Python 3 (`scripts/scrape.py`) executing via **GitHub Actions** (`.github/workflows/scrape.yml`) on a daily cron schedule.
* **Libraries & APIs:**
  * Scraper: `playwright`, `feedparser`, `requests`, TMDb API (The Movie Database).
  * Storage: `data/movies.json` (committed automatically by CI), client-side state (watchlist, exclusions, preferences) stored in browser `localStorage`.
* **Hosting:** GitHub Pages + GitHub Actions CI.

---

## 2. Directory Structure Map
```
newfeed/
├── index.html                   # Single-file frontend UI (HTML, embedded CSS, and client-side JS)
├── data/
│   └── movies.json              # Enriched movie catalog generated and updated by the scraper
├── scripts/
│   ├── scrape.py                # Python RSS scraper & TMDb enrichment pipeline
│   └── requirements.txt         # Dependencies for the GitHub Actions scraper runner
├── .github/
│   └── workflows/
│       └── scrape.yml           # Scheduled GitHub Action workflow
├── AGENTS.md                    # This agent reference document
└── .agents/rules/               # Workspace agent rules
```

---

## 3. Core Architectural & Development Rules

### A. Frontend Architecture (`index.html`)
* **Zero Build Pipeline:** `index.html` is standalone. Never introduce build tools (Webpack, Vite, Rollup, Babel, npm dependencies) to the frontend unless explicitly instructed.
* **Dynamic Rendering:** Data is fetched client-side from `data/movies.json`. Handle loading states, empty search results, and missing poster fallbacks cleanly.
* **Client-Side State:** Use `localStorage` for personal watchlists, exclusions, and UI theme preferences.
* **Responsive Design:** Ensure mobile-friendly, fluid grid layouts with clean modal dialogs for movie details and trailer embeds.

### B. Scraper & CI Automation (`scripts/scrape.py`)
* **Robust Error Handling:** The scraper must handle network timeouts, rate limits from TMDb, and malformed RSS feed items gracefully without crashing the CI run.
* **Secret Management:** Secrets like `TMDB_API_KEY` are injected via GitHub Actions environment variables. NEVER hardcode API keys in source files.
* **Data Sanitization:** Ensure `data/movies.json` adheres to a strict JSON structure with sanitized string fields, valid numeric years/ratings, and properly formatted image URLs.

---

## 4. Verification & Testing Checklist

Before submitting changes:
1. **Frontend Check:** Run `python -m http.server 8000` and test `http://localhost:8000` to verify rendering, filtering, and responsive behavior.
2. **Scraper Script Check:** Run `python -m py_compile scripts/scrape.py` to ensure clean Python syntax.
