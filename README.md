# rolling-reader

Local-first web scraper that automatically rolls through HTTP → browser → JS state extraction.

## Install

```bash
pip install rolling-reader
```

Python 3.11+. No Node.js required.

> **Note:** `playwright install chromium` is **not needed**. rolling-reader connects to your
> existing Chrome browser — it does not download or manage its own browser.

## Quick start

**Static pages — works immediately after install:**

```bash
rr https://news.ycombinator.com/
rr https://arxiv.org/abs/1706.03762 --clean   # article body only
```

**SPA / login-required pages — requires Chrome running with remote debugging:**

```bash
# Step 1: start Chrome with remote debugging (do this once per session)
#   macOS:   open -a "Google Chrome" --args --remote-debugging-port=9222
#   Windows: chrome --remote-debugging-port=9222
#   Linux:   google-chrome --remote-debugging-port=9222

# Step 2: scrape — rolling-reader reuses your existing session and cookies
rr https://app.example.com/dashboard
```

## How it works

| Level | Trigger | Speed |
|-------|---------|-------|
| 1 HTTP | Standard SSR page | ~500 ms |
| 2 CDP | SPA, JS rendering required, or auth-gated | ~3 s |
| 3 JS State | Next.js / Nuxt / Redux / Remix state variable detected | ~1 s (3–4× faster than Level 2 DOM) |

The dispatcher tries each level in order and stops at the first one that returns usable content.
Level 3 is attempted inside Level 2 — if a known JS state variable is found, DOM parsing is skipped entirely.

**Level 2 and 3 reuse your existing Chrome session**, including cookies and local storage.
No separate login step or credential storage required.

## CLI options

| Flag | Description |
|------|-------------|
| `--clean` / `-c` | Extract article body only (removes nav, ads, footers) |
| `--output json\|md` | Output format (default: json) |
| `--force-level 1\|2\|3` | Skip auto-detection, force a specific level |
| `--json-path <path>` | Extract a nested field, e.g. `title` or `props.pageProps` |
| `--no-cache` | Bypass profile cache, always re-explore |
| `--cdp <endpoint>` | Chrome DevTools endpoint (default: `http://localhost:9222`) |
| `--verbose` / `-v` | Print level selection and timing to stderr |

## Batch scraping

```bash
# Multiple URLs as arguments
rr batch https://example.com https://news.ycombinator.com/

# From a file (one URL per line, # for comments)
rr batch urls.txt

# Pipe-friendly: data goes to stdout, progress to stderr
rr batch urls.txt --clean > results.jsonl

# Control concurrency (default: 3)
rr batch urls.txt --concurrency 10
```

## Why not X

| Tool | Limitation |
|------|-----------|
| **Scrapling** | Cannot reuse an existing logged-in Chrome session; no JS state extraction |
| **Firecrawl** | Cloud API — data leaves your machine, metered pricing |
| **Jina Reader** | Cloud API — data leaves your machine, metered pricing |
| **rolling-reader** | Fully local, reuses your Chrome session and cookies, free forever |

## Supported JS state variables (v0.2+)

The following `window.*` variables are probed automatically for Level 3 extraction:

- `window.__NEXT_DATA__` — Next.js
- `window.__NUXT__` — Nuxt.js
- `window.__PRELOADED_STATE__` — Redux / custom
- `window.__INITIAL_STATE__` — various frameworks
- `window.__REDUX_STATE__` — Redux
- `window.__APP_STATE__` — various
- `window.__STATE__` — generic
- `window.__STORE__` — MobX / custom
- `window.APP_STATE` — no-underscore variant
- `window.initialState` — camelCase variant
- `window.__remixContext` — Remix
- `window.__staticRouterHydrationData` — React Router v6 SSR

Unknown variables matching `window.VAR = {…}` are also detected via automatic scan.

## License

MIT
