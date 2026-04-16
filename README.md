# rolling-reader

Local-first web scraper that automatically rolls through HTTP → browser → JS state extraction.

## Install

```bash
pip install rolling-reader
playwright install chromium  # required for Level 2 / Level 3
```

Python 3.11+. No Node.js required.

## Quick start

**Static page** (Level 1, no browser needed):

```bash
rr https://news.ycombinator.com/
```

**SPA or login-required page** (Level 2, reuses your existing Chrome session):

```bash
# 1. Start Chrome with remote debugging enabled (see section below)
# 2. Run the command — Level 2 is selected automatically
rr https://app.example.com/dashboard
```

**Output as Markdown:**

```bash
rr https://example.com --output md
```

## How it works

| Level | Trigger | Speed |
|-------|---------|-------|
| 1 HTTP | Standard SSR page, no JS rendering needed | ~500 ms |
| 2 CDP | SPA, JS rendering required, or auth-gated | ~3 s |
| 3 JS State | Next.js / Nuxt / Redux / Remix state variable detected | ~1 s (3–4x faster than Level 2 DOM parse) |

The dispatcher probes each level in order and stops at the first one that returns usable content. Level 3 is attempted after Level 2 attaches to the browser — if a known JS state variable is found, DOM parsing is skipped entirely.

## Starting Chrome for Level 2 / Level 3

Chrome must be running with remote debugging before invoking Level 2 or Level 3:

```bash
# macOS
open -a "Google Chrome" --args --remote-debugging-port=9222

# Windows
chrome --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

The existing Chrome session (including cookies and local storage) is reused — no separate login step required.

## CLI options

| Flag | Values | Description |
|------|--------|-------------|
| `--output` | `json`, `md` | Output format (default: plain text) |
| `--force-level` | `1`, `2`, `3` | Skip auto-detection, force a specific level |
| `--json-path` | dot-notation string | Extract a nested key from JSON output, e.g. `title` or `props.pageProps` |
| `--no-cache` | — | Disable response cache |
| `--cdp` | — | Force CDP connection (equivalent to `--force-level 2`) |
| `--verbose` | — | Print level selection reasoning and timing |

## Why not X

| Tool | Limitation |
|------|-----------|
| **Scrapling** | Cannot reuse an existing logged-in Chrome session; no JS state extraction |
| **Firecrawl** | Cloud API — data leaves your machine, metered pricing |
| **Jina Reader** | Cloud API — data leaves your machine, metered pricing |
| **rolling-reader** | Fully local, reuses your Chrome session and cookies, free forever |

## Supported JS state variables (v0.2)

The following `window.*` variables are probed automatically for Level 3 extraction:

- `window.__NEXT_DATA__` — Next.js (Vercel ecosystem)
- `window.__NUXT__` — Nuxt.js
- `window.__PRELOADED_STATE__` — Redux / custom
- `window.__INITIAL_STATE__` — various frameworks
- `window.__REDUX_STATE__` — Redux explicit naming
- `window.__APP_STATE__` — various frameworks
- `window.__STATE__` — generic
- `window.__STORE__` — MobX / custom
- `window.APP_STATE` — no-underscore variant
- `window.initialState` — camelCase variant
- `window.__remixContext` — Remix
- `window.__staticRouterHydrationData` — React Router v6 SSR

Unknown variables matching the pattern `window.VAR = {…}` are also detected via regex scan.

## License

MIT
