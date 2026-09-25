---
description: "Coding standards and rules for Scene Reel (newfeed)"
globs: "**/*"
---

# Scene Reel — Development Rules

1. **Zero-Build Frontend**:
   - `index.html` must remain self-contained (Vanilla HTML/CSS/JS).
   - Do not add npm packages or a bundler to the frontend.

2. **Scraper Resilience**:
   - `scripts/scrape.py` runs unattended in CI. All HTTP and Playwright calls must have timeouts and fallback handling.
   - Never commit sensitive API keys or tokens.

3. **Client State**:
   - Store user preferences, exclusions, and watchlists strictly in `localStorage`.
