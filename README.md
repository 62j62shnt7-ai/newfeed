# Scene Reel

A static movie-tracker: a scheduled GitHub Action scrapes your RSS feed,
enriches each entry with OMDb, and commits the result to `data/movies.json`.
`index.html` is a single-file vanilla JS/CSS page that reads that JSON and
renders the library — no server, no database, no Streamlit, no Selenium.

## What changed vs. the desktop app

| Desktop app (Streamlit) | This version |
|---|---|
| SQLite database | `data/movies.json`, committed by the Action |
| `undetected_chromedriver` + manual chromedriver version detection/download/code-signing | Playwright, which manages its own browser binary — none of that code exists anymore |
| Poster files cached to disk | OMDb poster URLs are hotlinked directly |
| Watchlist / exclusions stored in SQLite (server-side, shared) | Stored in the visitor's browser (`localStorage`) — personal, per-browser, not synced across devices |
| OMDb API key embedded in the app | Kept as a GitHub Actions secret — never shipped to the browser, since only the Action calls OMDb |

One behavior change worth knowing: excluding a movie in the old app deleted
it from the database so it could never come back. Here, "Exclude" just hides
it in your browser (and "Restore" is instant, no re-fetch needed) — the
scraper doesn't know about exclusions, so it won't re-scrape a title you
excluded, but the row does stay in `data/movies.json`.

## Setup

1. **Create the repo** and push these files (or point an existing repo at
   this content).

2. **Add your OMDb API key as a secret**
   Repo → Settings → Secrets and variables → Actions → New repository
   secret → name it `OMDB_API_KEY`.

3. *(Optional)* **Set repo variables** if you want to override the defaults
   in `scripts/scrape.py`:
   Repo → Settings → Secrets and variables → Actions → Variables tab
   - `FEED_URL` — defaults to `https://www.scnsrc.me/category/films/feed`
   - `MAX_PAGES` — defaults to `5`

4. **Enable GitHub Pages**
   Repo → Settings → Pages → Source: "Deploy from a branch" → Branch:
   `main` / root. Your site will be live at
   `https://<username>.github.io/<repo>/`.

5. **Run the scraper once manually** so there's data to show:
   Repo → Actions → "Update movie feed" → Run workflow.
   After it finishes, `data/movies.json` will have content and Pages will
   pick it up automatically.

It then runs daily at 07:00 UTC on its own (edit the `cron` line in
`.github/workflows/scrape.yml` to change that).

## Local preview

No build step — just serve the folder and open it:

```bash
python3 -m http.server 8000
# visit http://localhost:8000
```

(The scraper (`scripts/scrape.py`) is meant to run in CI, but you can run it
locally too: `pip install -r scripts/requirements.txt && playwright install
chromium && OMDB_API_KEY=... python scripts/scrape.py`.)
