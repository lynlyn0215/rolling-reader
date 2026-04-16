# Code Review Request — rolling-reader v0.6.3

## 项目简介

**rolling-reader**（CLI 命令 `rr`）是一个本地优先的网页内容提取工具，已发布到 PyPI。
核心价值主张：自动在三个层级之间选择最优抓取策略，并能复用用户已登录的 Chrome session。

```
Level 1 — HTTP 直取（httpx + trafilatura）        ~500ms
Level 2 — CDP + 用户已有的 Chrome session         ~3s
Level 3 — JS State 提取（window.__NEXT_DATA__ 等）~1s
```

用法示例：
```bash
rr https://example.com              # 自动选层级
rr https://example.com --clean      # trafilatura 正文过滤
rr batch urls.txt --concurrency 5   # 并发批量
rr chrome                           # 启动 rr 专用 Chrome（调试模式）
```

依赖：`httpx`, `beautifulsoup4`, `typer`, `playwright`, `trafilatura`

---

## 审核范围

请从以下几个维度做全面审核，给出具体的代码行级别反馈：

1. **逻辑 Bug** — 有没有明显的错误或边界条件遗漏？
2. **`needs_browser()` 准确率** — V3 的启发式判断是否有漏洞或过度触发的场景？
3. **异步/并发安全** — `rr batch` 的并发实现有没有问题？
4. **错误处理** — 异常类型设计是否合理？有没有过宽的 `except Exception` 吞掉了重要错误？
5. **Profile Cache** — 缓存逻辑是否正确？有没有竞态条件（多进程并发写同一 domain.json）？
6. **API 设计** — 作为 PyPI 包，`dispatch()` 作为公开 API 是否设计合理？有没有需要补充的参数或返回值？
7. **已知问题确认** — 下面我列了几个自己发现的问题，请确认是否确实是问题，并评估严重程度

### 我发现的疑似问题（请确认）

**问题 A**：`dispatcher.py` 第 121 行
```python
state_var = KNOWN_STATE_VARS[0]   # v0.1 固定
```
实际命中的 `state_var` 由 `try_extract_state()` 返回，但这里保存到 Profile Cache 时硬编码了 `KNOWN_STATE_VARS[0]`（即 `window.__NEXT_DATA__`），如果某站点实际用的是第 3 个变量，下次缓存命中时读到的 `state_var` 是错的。

**问题 B**：`cdp.py` 第 186 行
```python
text = cleaned if cleaned else _extract_text(BeautifulSoup(html, "html.parser"))
```
上面第 182 行已经 `soup = BeautifulSoup(html, "html.parser")`，但这里又重新 parse 了一次，双倍开销。

**问题 C**：`dispatcher.py` 顶部注释写「v0.1 只支持到 Level 2」，实际已经有 Level 3，注释过期。

**问题 D**：`profile.py` 的 `invalidate()` 不删除文件，而是把 `discovered_at` 清空为 `""`，然后在 `load()` 里用 `datetime.fromisoformat("")` — 这会抛 `ValueError`，被 `except ValueError: pass` 吞掉，最终 `load()` 返回这条失效记录而不是 `None`，缓存失效逻辑实际上不工作。请确认。

---

## 完整源码

### `models.py`

```python
"""
rolling_reader/models.py
共享数据类型：ExtractResult、异常类。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


class NeedsBrowserError(Exception):
    """Level 1 检测到页面需要浏览器渲染，应升级到 Level 2。"""
    def __init__(self, url: str, reason: str = ""):
        self.url = url
        self.reason = reason
        super().__init__(f"Browser required for {url}: {reason}")


class ExtractionError(Exception):
    """抓取过程中发生不可恢复的错误。"""
    def __init__(self, url: str, reason: str = ""):
        self.url = url
        self.reason = reason
        super().__init__(f"Extraction failed for {url}: {reason}")


@dataclass
class ExtractResult:
    url: str
    level: int          # 1=HTTP, 2=CDP, 3=JS State
    status_code: int
    title: str
    text: str
    links: list[str]
    elapsed_ms: float
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "level": self.level,
            "status_code": self.status_code,
            "title": self.title,
            "text": self.text,
            "links": self.links,
            "elapsed_ms": self.elapsed_ms,
            "extracted_at": self.extracted_at,
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_markdown(self) -> str:
        lines = [
            f"# {self.title or self.url}", "",
            f"> URL: {self.url}  ",
            f"> Level: {self.level}  ",
            f"> Extracted: {self.extracted_at}",
            "", "---", "", self.text,
        ]
        if self.links:
            lines += ["", "---", "", "## Links", ""]
            lines += [f"- {link}" for link in self.links[:50]]
            if len(self.links) > 50:
                lines.append(f"- *(+{len(self.links) - 50} more)*")
        return "\n".join(lines)
```

---

### `dispatcher.py`

```python
"""
rolling_reader/dispatcher.py
核心调度器：按策略阶梯自动升级。

Level 1 → Level 2 → Level 3（v0.1 只支持到 Level 2）  ← 注释过期

升级条件：
  - Level 1 抛出 NeedsBrowserError → 升级到 Level 2/3
  - Level 1 抛出 ExtractionError  → 升级到 Level 2/3
  - --force-level N              → 跳过前面的层级
"""

from __future__ import annotations

import asyncio
from typing import Optional

from rolling_reader.models import ExtractResult, NeedsBrowserError, ExtractionError
from rolling_reader.extractor import http_extract, cdp_extract, is_chrome_available
from rolling_reader.cache import profile as profile_cache


async def dispatch(
    url: str,
    *,
    force_level: Optional[int] = None,
    cdp_endpoint: str = "http://localhost:9222",
    http_timeout: float = 15.0,
    page_timeout: float = 30.0,
    verbose: bool = False,
    use_cache: bool = True,
    clean: bool = False,
) -> ExtractResult:
    def log(msg: str) -> None:
        if verbose:
            print(f"[rolling-reader] {msg}", flush=True)

    if force_level == 1:
        log("forced Level 1 (HTTP)")
        return await http_extract(url, timeout=http_timeout, clean=clean)

    if force_level in (2, 3):
        log(f"forced Level 2/3 (CDP)")
        return await _try_level2(url, cdp_endpoint, page_timeout, log, clean=clean)

    if use_cache:
        cached = profile_cache.load(url)
        if cached:
            preferred = cached.get("preferred_level", 1)
            log(f"cache hit → Level {preferred} for {cached.get('domain')}")
            if preferred == 1:
                try:
                    result = await http_extract(url, timeout=http_timeout, clean=clean)
                    profile_cache.save(url, result.level)
                    return result
                except Exception:
                    log("cache: Level 1 failed, invalidating and re-exploring")
                    profile_cache.invalidate(url)
            else:
                try:
                    result = await _try_level2(url, cdp_endpoint, page_timeout, log, clean=clean)
                    profile_cache.save(url, result.level,
                                       state_var=cached.get("state_var"))
                    return result
                except Exception:
                    log("cache: Level 2 failed, invalidating and re-exploring")
                    profile_cache.invalidate(url)

    # Level 1
    log(f"Level 1 → {url}")
    try:
        result = await http_extract(url, timeout=http_timeout, clean=clean)
        log(f"Level 1 succeeded ({result.elapsed_ms:.0f}ms)")
        if use_cache:
            profile_cache.save(url, result.level)
        return result
    except NeedsBrowserError as e:
        log(f"Level 1 → needs browser ({e.reason}), escalating to Level 2/3")
    except ExtractionError as e:
        log(f"Level 1 → error ({e.reason}), escalating to Level 2/3")

    # Level 2/3
    result = await _try_level2(url, cdp_endpoint, page_timeout, log, clean=clean)
    if use_cache:
        state_var = None
        if result.level == 3:
            from rolling_reader.extractor.state import KNOWN_STATE_VARS
            state_var = KNOWN_STATE_VARS[0]   # ← 疑似 Bug A：硬编码了第 0 个
        profile_cache.save(url, result.level, state_var=state_var)
    return result


async def _try_level2(
    url: str,
    cdp_endpoint: str,
    page_timeout: float,
    log,
    *,
    clean: bool = False,
) -> ExtractResult:
    from rolling_reader.extractor.cdp import ChromeNotRunningError

    log(f"Level 2 → {url}")

    if not await is_chrome_available(cdp_endpoint):
        raise ExtractionError(
            url,
            f"Level 1 failed and Chrome is not available at {cdp_endpoint}. "
            "Start Chrome with: chrome --remote-debugging-port=9222",
        )

    try:
        result = await cdp_extract(
            url,
            cdp_endpoint=cdp_endpoint,
            page_timeout=page_timeout,
            clean=clean,
        )
        log(f"Level 2 succeeded ({result.elapsed_ms:.0f}ms)")
        return result
    except ChromeNotRunningError:
        raise
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(url, f"Level 2 unexpected error: {e}") from e
```

---

### `extractor/http.py`

```python
"""
rolling_reader/extractor/http.py
Level 1 — HTTP 直取

needs_browser() V3（经过 50+ URL 验证，准确率 96%）
改进：
  - 检查前剥离 <noscript>，避免 PyPI 类误报
  - 小页面误判修复：tlen < 200 同时要求 ratio < 0.15
  - 尺寸感知阈值：大页面（>50KB）用 < 0.018，小页面用 < 0.05
  - 4xx 直接升级
"""

from __future__ import annotations

import time
from urllib.parse import urljoin, urlparse
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from rolling_reader.models import ExtractResult, NeedsBrowserError, ExtractionError


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def needs_browser(response: httpx.Response) -> tuple[bool, str]:
    html = response.text

    if len(html) == 0:
        return False, ""

    if response.status_code in (400, 401, 403, 407):
        return True, f"http_{response.status_code}"

    if len(html) < 500:
        return True, "short_response"

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("noscript"):
        tag.decompose()
    cleaned_html = str(soup).lower()

    js_markers = [
        "enable javascript",
        "you need javascript",
        "javascript is required",
        "javascript is disabled",
        '<div id="app"></div>',
        "<div id='app'></div>",
        '<div id="root"></div>',
        "<div id='root'></div>",
    ]
    for marker in js_markers:
        if marker in cleaned_html:
            return True, f"js_marker:{marker[:30]}"

    text_content = soup.get_text(strip=True)
    text_len = len(text_content)
    html_len = len(html)
    text_ratio = text_len / max(html_len, 1)

    if text_ratio < 0.005:
        return True, f"ratio_near_zero:{text_ratio:.4f}"

    if text_len < 200 and text_ratio < 0.15:
        return True, f"tiny_shell:tlen={text_len}"

    if html_len > 50_000:
        if text_ratio < 0.018:
            return True, f"large_page_low_ratio:{text_ratio:.4f}"
    else:
        if text_ratio < 0.05:
            return True, f"small_page_low_ratio:{text_ratio:.4f}"

    return False, ""


def _extract_title(soup: BeautifulSoup) -> str:
    tag = soup.find("title")
    if tag:
        return tag.get_text(strip=True)
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return ""


def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    container = soup.find("main") or soup.find("article") or soup.find("body") or soup
    lines = [line.strip() for line in container.get_text(separator="\n").splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _extract_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
    return links


async def extract(
    url: str,
    *,
    timeout: float = 15.0,
    headers: Optional[dict] = None,
    client: Optional[httpx.AsyncClient] = None,
    clean: bool = False,
) -> ExtractResult:
    merged_headers = {**DEFAULT_HEADERS, **(headers or {})}

    async def _do_request(c: httpx.AsyncClient) -> ExtractResult:
        t0 = time.perf_counter()
        try:
            response = await c.get(url, follow_redirects=True)
        except httpx.TimeoutException as e:
            raise ExtractionError(url, f"timeout: {e}") from e
        except httpx.RequestError as e:
            raise ExtractionError(url, f"request error: {e}") from e
        elapsed = (time.perf_counter() - t0) * 1000

        browser_needed, reason = needs_browser(response)
        if browser_needed:
            raise NeedsBrowserError(url, reason)

        soup = BeautifulSoup(response.text, "html.parser")
        title = _extract_title(soup)
        links = _extract_links(soup, str(response.url))

        if clean:
            from rolling_reader.extractor.clean import clean_extract
            cleaned = clean_extract(response.text, url=str(response.url))
            text = cleaned if cleaned else _extract_text(soup)
        else:
            text = _extract_text(BeautifulSoup(response.text, "html.parser"))  # ← 重复 parse

        return ExtractResult(
            url=str(response.url),
            level=1,
            status_code=response.status_code,
            title=title,
            text=text,
            links=links,
            elapsed_ms=round(elapsed, 1),
        )

    if client is not None:
        return await _do_request(client)

    async with httpx.AsyncClient(
        headers=merged_headers,
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    ) as c:
        return await _do_request(c)
```

---

### `extractor/cdp.py`

```python
"""
rolling_reader/extractor/cdp.py
Level 2 — CDP + 已有 Chrome Session（Playwright connect_over_cdp）

流程：
  1. 连接 localhost:9222
  2. 取已有 context（继承登录态）
  3. 开新标签页，导航
  4. 等待 domcontentloaded + networkidle（可选）
  5. 尝试 Level 3 JS State 提取
  6. 回退到 Level 2 DOM 提取
  7. 关闭标签页
"""

from __future__ import annotations

import time
from typing import Optional

from bs4 import BeautifulSoup

from rolling_reader.models import ExtractResult, ExtractionError
from rolling_reader.extractor.http import _extract_title, _extract_text, _extract_links

CDP_ENDPOINT = "http://localhost:9222"
WAIT_UNTIL = "domcontentloaded"
NETWORK_IDLE_TIMEOUT = 5_000  # ms


class ChromeNotRunningError(ExtractionError):
    def __init__(self, endpoint: str = CDP_ENDPOINT):
        super().__init__(
            url="",
            reason=(
                f"Cannot connect to Chrome at {endpoint}. "
                "Start Chrome with: "
                "chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug"
            ),
        )


async def extract(
    url: str,
    *,
    cdp_endpoint: str = CDP_ENDPOINT,
    page_timeout: float = 30.0,
    wait_networkidle: bool = True,
    clean: bool = False,
) -> ExtractResult:
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError as e:
        raise ExtractionError(url, "playwright is not installed.") from e

    t0 = time.perf_counter()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_endpoint, timeout=5_000)
        except Exception as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ("connection refused", "connect", "econnrefused", "failed to connect")):
                raise ChromeNotRunningError(cdp_endpoint) from e
            raise ExtractionError(url, f"cdp connect error: {e}") from e

        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = await browser.new_context()

        page = await context.new_page()

        try:
            try:
                await page.goto(url, wait_until=WAIT_UNTIL, timeout=page_timeout * 1000)
            except PlaywrightTimeout as e:
                raise ExtractionError(url, f"page load timeout: {e}") from e
            except Exception as e:
                raise ExtractionError(url, f"navigation error: {e}") from e

            if wait_networkidle:
                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT)
                except PlaywrightTimeout:
                    pass  # 非致命

            from rolling_reader.extractor.state import try_extract_state, state_to_text
            state_var, state_data = await try_extract_state(page)

            html = await page.content()
            final_url = page.url

        finally:
            await page.close()

    elapsed = (time.perf_counter() - t0) * 1000

    if state_data is not None:
        soup = BeautifulSoup(html, "html.parser")
        return ExtractResult(
            url=final_url, level=3, status_code=200,
            title=_extract_title(soup),
            text=state_to_text(state_var, state_data),
            links=_extract_links(soup, final_url),
            elapsed_ms=round(elapsed, 1),
        )

    soup = BeautifulSoup(html, "html.parser")
    if clean:
        from rolling_reader.extractor.clean import clean_extract
        cleaned = clean_extract(html, url=final_url)
        text = cleaned if cleaned else _extract_text(BeautifulSoup(html, "html.parser"))  # ← 疑似 Bug B：重复 parse
    else:
        text = _extract_text(BeautifulSoup(html, "html.parser"))  # ← 同上
    return ExtractResult(
        url=final_url, level=2, status_code=200,
        title=_extract_title(soup),
        text=text,
        links=_extract_links(soup, final_url),
        elapsed_ms=round(elapsed, 1),
    )


async def is_chrome_available(cdp_endpoint: str = CDP_ENDPOINT) -> bool:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{cdp_endpoint}/json/version")
            return resp.status_code == 200
    except Exception:
        return False
```

---

### `extractor/state.py`

```python
"""
rolling_reader/extractor/state.py
Level 3 — JS State 提取

策略：
  1. 逐一尝试 KNOWN_STATE_VARS（12 个，按常见度排序）
  2. 若均未命中，auto_scan_state() 扫描 <script> 里的未知大 JSON 对象
  3. 框架感知：__NEXT_DATA__ 自动钻入 props.pageProps
"""

from __future__ import annotations

import json
import re
from typing import Optional, Any


KNOWN_STATE_VARS: list[str] = [
    "window.__NEXT_DATA__",
    "window.__NUXT__",
    "window.__PRELOADED_STATE__",
    "window.__INITIAL_STATE__",
    "window.__REDUX_STATE__",
    "window.__APP_STATE__",
    "window.__STATE__",
    "window.__STORE__",
    "window.APP_STATE",
    "window.initialState",
    "window.__remixContext",
    "window.__staticRouterHydrationData",
]

_AUTO_SCAN_MIN_BYTES = 1_000

_WINDOW_VAR_RE = re.compile(
    r'window\.([A-Za-z_$][A-Za-z0-9_$]*)(?:\s*[=:]\s*)(\{|\[)',
    re.MULTILINE,
)

_FRAMEWORK_EXTRACTORS: dict[str, list[str]] = {
    "window.__NEXT_DATA__": ["props", "pageProps"],
    "window.__NUXT__": ["data"],
    "window.__remixContext": ["state", "loaderData"],
}


def _deep_get(data: Any, path: list[str]) -> Any:
    current = data
    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return data
    return current


async def try_extract_state(
    page,
    state_vars: list[str] = KNOWN_STATE_VARS,
    auto_scan: bool = True,
) -> tuple[Optional[str], Optional[Any]]:
    for var in state_vars:
        try:
            data = await page.evaluate(f"() => {var}")
            if data is not None:
                if var in _FRAMEWORK_EXTRACTORS:
                    path = _FRAMEWORK_EXTRACTORS[var]
                    data = _deep_get(data, path)
                return var, data
        except Exception:
            continue

    if auto_scan:
        return await auto_scan_state(page)

    return None, None


async def auto_scan_state(page) -> tuple[Optional[str], Optional[Any]]:
    try:
        html = await page.content()
    except Exception:
        return None, None

    candidates: list[str] = []
    seen: set[str] = set()
    known_set = set(KNOWN_STATE_VARS)

    for match in _WINDOW_VAR_RE.finditer(html):
        var_name = f"window.{match.group(1)}"
        if var_name not in known_set and var_name not in seen:
            raw = match.group(1)
            if raw in _BROWSER_BUILTINS:
                continue
            candidates.append(var_name)
            seen.add(var_name)

    for var in candidates:
        try:
            data = await page.evaluate(f"() => {var}")
            if data is None:
                continue
            serialized = json.dumps(data, ensure_ascii=False)
            if len(serialized.encode()) >= _AUTO_SCAN_MIN_BYTES:
                return var, data
        except Exception:
            continue

    return None, None


_BROWSER_BUILTINS: frozenset[str] = frozenset({
    "addEventListener", "alert", "atob", "blur", "btoa",
    "clearInterval", "clearTimeout", "close", "closed",
    "confirm", "console", "crypto", "customElements",
    "devicePixelRatio", "dispatchEvent", "document",
    "fetch", "focus", "frameElement", "frames",
    "getComputedStyle", "getSelection", "history",
    "indexedDB", "innerHeight", "innerWidth",
    "length", "localStorage", "location",
    "matchMedia", "moveTo", "name", "navigator",
    "onload", "open", "opener", "origin", "outerHeight",
    "outerWidth", "pageXOffset", "pageYOffset",
    "parent", "performance", "postMessage", "print",
    "prompt", "removeEventListener", "requestAnimationFrame",
    "resizeTo", "screen", "screenLeft", "screenTop",
    "screenX", "screenY", "scroll", "scrollBy",
    "scrollTo", "scrollX", "scrollY", "self",
    "sessionStorage", "setInterval", "setTimeout",
    "speechSynthesis", "status", "stop", "top",
    "visualViewport", "window",
    "ga", "gtag", "dataLayer", "fbq", "twq",
    "Intercom", "analytics", "mixpanel", "amplitude",
})


def state_to_text(var_name: str, data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
```

---

### `cache/profile.py`

```python
"""
rolling_reader/cache/profile.py
Profile Cache — 按 domain 缓存最优抓取策略

存储：~/.rolling-reader/profiles/<domain>.json
有效期：30 天
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

CACHE_DIR = Path.home() / ".rolling-reader" / "profiles"
STALE_DAYS = 30


def _domain(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host.lower()


def _profile_path(domain: str) -> Path:
    return CACHE_DIR / f"{domain}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(url: str) -> Optional[dict]:
    domain = _domain(url)
    path = _profile_path(domain)

    if not path.exists():
        return None

    try:
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    discovered = profile.get("discovered_at", "")
    if discovered:
        try:
            dt = datetime.fromisoformat(discovered)
            if datetime.now(timezone.utc) - dt > timedelta(days=STALE_DAYS):
                path.unlink(missing_ok=True)
                return None
        except ValueError:
            pass  # ← 疑似 Bug D：discovered_at="" 时 fromisoformat 抛 ValueError，
                  #   被 pass 吞掉，继续返回这条失效的 profile

    return profile


def save(url: str, result_level: int, state_var: Optional[str] = None) -> None:
    domain = _domain(url)
    path = _profile_path(domain)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    now = _now_iso()
    profile = {
        "domain": domain,
        "preferred_level": result_level,
        "state_var": state_var,
        "discovered_at": existing.get("discovered_at", now),
        "last_success": now,
        "success_count": existing.get("success_count", 0) + 1,
        "failure_count": existing.get("failure_count", 0),
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def invalidate(url: str) -> None:
    domain = _domain(url)
    path = _profile_path(domain)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                profile = json.load(f)
            profile["failure_count"] = profile.get("failure_count", 0) + 1
            profile["discovered_at"] = ""   # ← 疑似 Bug D：清空为空字符串而非删除文件
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile, f, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
```

---

## 期望输出格式

请按以下格式组织反馈：

### 确认的 Bug（严重 / 中等 / 轻微）
每个 bug：问题描述 + 建议修复代码

### 新发现的问题
同上格式

### needs_browser() 评估
列出你认为会误判的场景（假阳性 / 假阴性各举 2-3 个）

### API 设计建议
作为 PyPI 包，`dispatch()` 有哪些改进空间

### 不需要改的
哪些看起来有问题但实际上是合理的设计决策

---

谢谢。
