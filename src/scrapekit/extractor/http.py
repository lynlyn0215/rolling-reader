"""
scrapekit/extractor/http.py
============================
Level 1 — HTTP 直取（httpx + beautifulsoup4）

核心职责：
  1. 发起 HTTP 请求
  2. 通过 needs_browser() 判断是否需要升级
  3. 提取 title、正文、链接
  4. 返回 ExtractResult，或 raise NeedsBrowserError

needs_browser() 版本：V3（经过 50+ URL 验证，准确率 96%）
关键改进：
  - 检查前剥离 <noscript>，避免 PyPI 类误报
  - 小页面误判修复：tlen < 200 同时要求 ratio < 0.15
  - 尺寸感知阈值：大页面（>50KB）用 < 0.018，小页面用 < 0.05
  - 4xx 直接升级（Level 1 已失败）
"""

from __future__ import annotations

import time
from urllib.parse import urljoin, urlparse
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from scrapekit.models import ExtractResult, NeedsBrowserError, ExtractionError


# ---------------------------------------------------------------------------
# 请求头（模拟真实 Chrome，减少 bot 拦截）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# needs_browser() — V3
# ---------------------------------------------------------------------------

def needs_browser(response: httpx.Response) -> tuple[bool, str]:
    """
    判断 HTTP 响应是否需要浏览器渲染。

    Returns:
        (needs_browser: bool, reason: str)
    """
    html = response.text

    # 1. 空 body → API 端点，不是 SPA
    if len(html) == 0:
        return False, ""

    # 2. 4xx → Level 1 已失败，升级（Chrome 通常能绕过 bot 检测 / 登录墙）
    if response.status_code in (400, 401, 403, 407):
        return True, f"http_{response.status_code}"

    # 3. 很短的 2xx → SPA shell
    if len(html) < 500:
        return True, "short_response"

    # 4. 解析 HTML，去掉 <noscript>（避免 noscript 里的功能提示触发误判）
    #    反例：PyPI 在 <noscript> 里写 "Enable javascript to filter wheels"
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("noscript"):
        tag.decompose()
    cleaned_html = str(soup).lower()

    # 5. 显式 JS 要求标记
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

    # 6. 文本内容分析
    text_content = soup.get_text(strip=True)
    text_len = len(text_content)
    html_len = len(html)                        # 分母用原始 HTML 保持一致
    text_ratio = text_len / max(html_len, 1)

    # 6a. 极低比例 → 肯定是 SPA（Instagram / YouTube 类型）
    if text_ratio < 0.005:
        return True, f"ratio_near_zero:{text_ratio:.4f}"

    # 6b. 文字量极少 + ratio 也低 → SPA shell
    #     example.com(tlen=139, ratio=0.263) 不应被触发
    #     Facebook(tlen=111, ratio=0.072) 应被触发
    if text_len < 200 and text_ratio < 0.15:
        return True, f"tiny_shell:tlen={text_len}"

    # 6c. 尺寸感知 ratio 阈值
    #     大页面天然 ratio 偏低（大量 HTML 标签）
    #     < 0.018：覆盖 Airtable(0.015)/Notion(0.015)/Replit(0.014) 等 SPA
    #              不触发 GitHub(0.019)/BBC(0.031)/PyPI(0.075)
    if html_len > 50_000:
        if text_ratio < 0.018:
            return True, f"large_page_low_ratio:{text_ratio:.4f}"
    else:
        if text_ratio < 0.05:
            return True, f"small_page_low_ratio:{text_ratio:.4f}"

    return False, ""


# ---------------------------------------------------------------------------
# 内容提取
# ---------------------------------------------------------------------------

def _extract_title(soup: BeautifulSoup) -> str:
    tag = soup.find("title")
    if tag:
        return tag.get_text(strip=True)
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return ""


def _extract_text(soup: BeautifulSoup) -> str:
    """
    提取页面主要文字内容。
    去除 script / style / noscript，保留段落文字。
    """
    for tag in soup.find_all(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    # 优先从 <main> / <article> 提取；否则用 <body>
    container = soup.find("main") or soup.find("article") or soup.find("body") or soup
    lines = [line.strip() for line in container.get_text(separator="\n").splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _extract_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """提取所有 <a href> 链接，转为绝对 URL，去重。"""
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


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

async def extract(
    url: str,
    *,
    timeout: float = 15.0,
    headers: Optional[dict] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> ExtractResult:
    """
    Level 1 HTTP 抓取。

    Args:
        url:     目标 URL
        timeout: 请求超时秒数
        headers: 额外请求头（会合并到 DEFAULT_HEADERS）
        client:  可复用的 httpx.AsyncClient（不传则自动创建）

    Returns:
        ExtractResult

    Raises:
        NeedsBrowserError: 页面需要浏览器渲染
        ExtractionError:   请求或解析失败
    """
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

        # 判断是否需要浏览器
        browser_needed, reason = needs_browser(response)
        if browser_needed:
            raise NeedsBrowserError(url, reason)

        # 解析内容
        soup = BeautifulSoup(response.text, "html.parser")
        title = _extract_title(soup)
        text  = _extract_text(BeautifulSoup(response.text, "html.parser"))  # 用新 soup 避免修改影响
        links = _extract_links(soup, str(response.url))

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
