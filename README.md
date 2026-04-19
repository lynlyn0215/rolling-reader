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
rr https://arxiv.org/abs/1706.03762 --clean      # article body only
rr https://api.github.com/repos/owner/repo       # REST API — returns JSON directly
```

**SPA / login-required pages — requires Chrome:**

```bash
rr chrome                          # launch Chrome with remote debugging (do once per session)
rr https://app.example.com/dashboard
```

## How it works

| Level | Trigger | Speed |
|-------|---------|-------|
| 1 HTTP | Standard SSR page or REST API | ~500 ms |
| 2 CDP | SPA, JS rendering required, or auth-gated | ~3 s |
| 3 JS State | Next.js / Nuxt / Redux / Remix state variable detected | ~1 s (3–4× faster than Level 2) |

The dispatcher tries each level in order and stops at the first one that returns usable content.
Level 3 is attempted inside Level 2 — if a known JS state variable is found, DOM parsing is skipped entirely.

**Level 2 and 3 reuse your existing Chrome session**, including cookies and local storage.
No separate login step or credential storage required.

A **Profile Cache** records the best level per domain so subsequent requests skip re-exploration.
Cache expires 7 days after last success; L2/3 sites are silently re-probed at L1 every 20 requests
to detect if they've since added SSR.

## CLI reference

### Single URL

```bash
rr <url> [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--clean` / `-c` | Extract article body only (removes nav, ads, footers) |
| `--select` / `-s` | CSS selector — extract only matching elements, e.g. `"article"`, `".post-body"`, `"h1,p"` |
| `--text` | Output plain text only to stdout (pipeline-friendly: `rr <url> --text \| llm`) |
| `--meta` | Extract structured metadata: og:*, JSON-LD, published time, author, canonical URL |
| `--images` | Extract og:image + article images |
| `--rss` | Parse RSS 2.0 / Atom feeds into a structured JSON array |
| `--output json\|md` | Output format (default: json) |
| `--json-path <path>` | Extract a nested field, e.g. `title` or `props.pageProps` |
| `--force-level 1\|2\|3` | Skip auto-detection, force a specific level |
| `--retries N` | Max retries on 429 / 503 with exponential backoff (default: 2) |
| `--no-cache` | Bypass profile cache, always re-explore |
| `--cdp <endpoint>` | Chrome DevTools endpoint (default: `http://localhost:9222`) |
| `--verbose` / `-v` | Print level selection and timing to stderr |

### Batch

```bash
rr batch <urls or file> [OPTIONS]
```

```bash
rr batch https://example.com https://hn.algolia.com   # inline URLs
rr batch urls.txt --clean > results.jsonl             # from file, pipe output
cat urls.txt | rr batch --clean                       # from stdin
rr batch urls.txt --concurrency 10                    # control parallelism (default: 3)
```

### Chrome

```bash
rr chrome          # launch Chrome with remote debugging on port 9222
rr chrome --fresh  # start with a clean profile (clears saved logins)
```

Login state is persisted in `~/.rolling-reader/chrome-profile/`. Log in once, stay logged in.

## Common patterns

```bash
# Article body as plain text → pipe to any LLM
rr https://example.com/article --clean --text | llm summarize

# Only the metadata (published date, author, og:image)
rr https://example.com/article --meta --json-path meta

# Extract a specific section by CSS selector
rr https://example.com --select "article.post-content"

# Fetch a REST API endpoint
rr https://api.github.com/repos/owner/repo --json-path name

# Parse an RSS feed
rr https://hnrss.org/frontpage --rss

# Scrape a page that needs login
rr chrome   # log in manually once
rr https://app.example.com/dashboard --clean
```

## Why not X

| Tool | Limitation |
|------|-----------|
| **Scrapling** | Cannot reuse an existing logged-in Chrome session; no JS state extraction |
| **Firecrawl** | Cloud API — data leaves your machine, metered pricing |
| **Jina Reader** | Cloud API — data leaves your machine, metered pricing |
| **rolling-reader** | Fully local, reuses your Chrome session and cookies, free forever |

## Supported JS state variables (Level 3)

Probed automatically:

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

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT
