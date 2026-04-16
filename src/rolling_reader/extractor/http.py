"""
rolling_reader/extractor/http.py
============================
Level 1 — HTTP 直取（httpx + beautifulsoup4）

核心职责：
  1. 发起 HTTP 请求
  2. 通过 needs_browser() 判断是否需要升级
  3. 提取 title、正文、链接（或嵌入 JS state）
  4. 返回 ExtractResult，或 raise NeedsBrowserError

needs_browser() 版本：V4
新增：
  - Content-Type application/json → 直接 False（API 端点）
  - 嵌入 state 保险（含 __NEXT_DATA__ 的页面不升级，L1 直接提取）
  - 空 main 容器检测（SPA 有 nav/footer 但 <main> 为空）
沿用 V3：
  - 剥离 <noscript> 避免误报
  - 小页面误判修复
  - 尺寸感知 ratio 阈值
  - 4xx 直接升级
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
from typing import Optional
import json

import httpx
from bs4 import BeautifulSoup

from rolling_reader.models import ExtractResult, NeedsBrowserError, ExtractionError


# ---------------------------------------------------------------------------
# RSS / Atom 检测与结构化解析
# ---------------------------------------------------------------------------

_RSS_NS = {
    "dc":      "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media":   "http://search.yahoo.com/mrss/",
    "atom":    "http://www.w3.org/2005/Atom",
}


def _is_feed(response: httpx.Response) -> bool:
    """判断响应是否为 RSS / Atom feed。"""
    ct = response.headers.get("content-type", "").lower()
    if any(x in ct for x in ("application/rss", "application/atom", "application/xml", "text/xml")):
        return True
    # Content-Type 不可靠时，看前 200 字节
    snippet = response.text[:200].lstrip()
    return snippet.startswith("<?xml") or "<rss" in snippet or "<feed" in snippet


def _parse_feed(text: str, base_url: str) -> list[dict]:
    """
    解析 RSS 2.0 / Atom feed，返回 item 列表。
    每个 item 包含：title, link, description, pub_date, author, categories
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    items: list[dict] = []

    # ── RSS 2.0 ──────────────────────────────────────────────────────────
    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item"):
            def rss_get(tag: str, ns: str = "") -> str:
                el = item.find(f"{{{ns}}}{tag}" if ns else tag)
                return (el.text or "").strip() if el is not None else ""

            cats = [c.text.strip() for c in item.findall("category") if c.text]
            items.append({
                "title":       rss_get("title"),
                "link":        rss_get("link"),
                "description": rss_get("description"),
                "pub_date":    rss_get("pubDate"),
                "author":      rss_get("creator", _RSS_NS["dc"]) or rss_get("author"),
                "categories":  cats,
            })
        return items

    # ── Atom ─────────────────────────────────────────────────────────────
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        def atom_get(tag: str) -> str:
            el = entry.find(f"a:{tag}", ns)
            return (el.text or "").strip() if el is not None else ""

        link_el = entry.find("a:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        items.append({
            "title":       atom_get("title"),
            "link":        link,
            "description": atom_get("summary") or atom_get("content"),
            "pub_date":    atom_get("updated") or atom_get("published"),
            "author":      atom_get("name"),
            "categories":  [],
        })

    return items


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

    # 2. Content-Type: application/json → API 端点，永远不需要浏览器
    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        return False, ""

    # 3. 4xx → Level 1 已失败，升级（Chrome 通常能绕过 bot 检测 / 登录墙）
    if response.status_code in (400, 401, 403, 407):
        return True, f"http_{response.status_code}"

    # 4. 很短的 2xx → SPA shell
    if len(html) < 500:
        return True, "short_response"

    # 5. 解析 HTML，去掉 <noscript>（避免 noscript 里的功能提示触发误判）
    #    反例：PyPI 在 <noscript> 里写 "Enable javascript to filter wheels"
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("noscript"):
        tag.decompose()
    cleaned_html = str(soup).lower()

    # 6. 显式 JS 要求标记
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

    # 7. 文本内容分析
    text_content = soup.get_text(strip=True)
    text_len = len(text_content)
    html_len = len(html)                        # 分母用原始 HTML 保持一致
    text_ratio = text_len / max(html_len, 1)

    # 7a. 嵌入 state 保险（在所有 ratio 判定之前）
    #     含 __NEXT_DATA__ 等嵌入 state 的页面，即使 ratio 极低也不需要浏览器
    #     L1 直接从 <script> 标签提取结构化数据
    from rolling_reader.extractor.state import has_embedded_state
    if has_embedded_state(html):
        return False, ""

    # 7b. 极低比例 → 肯定是 SPA（Instagram / YouTube 类型）
    if text_ratio < 0.005:
        return True, f"ratio_near_zero:{text_ratio:.4f}"

    # 7b. 文字量极少 + ratio 也低 → SPA shell
    #     example.com(tlen=139, ratio=0.263) 不应被触发
    #     Facebook(tlen=111, ratio=0.072) 应被触发
    if text_len < 200 and text_ratio < 0.15:
        return True, f"tiny_shell:tlen={text_len}"

    # 7c. 尺寸感知 ratio 阈值
    #     大页面天然 ratio 偏低（大量 HTML 标签）
    #     < 0.018：覆盖 Airtable(0.015)/Notion(0.015)/Replit(0.014) 等 SPA
    #              不触发 GitHub(0.019)/BBC(0.031)/PyPI(0.075)
    if html_len > 50_000:
        if text_ratio < 0.018:
            return True, f"large_page_low_ratio:{text_ratio:.4f}"
    else:
        if text_ratio < 0.05:
            return True, f"small_page_low_ratio:{text_ratio:.4f}"

    # 8. 空 main 容器检测：SPA 常见模式——nav/footer 有文字，但 <main> 是空的
    #    例：React/Vue app 初始渲染前，<main> 里只有 loading spinner
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main:
        main_text = main.get_text(strip=True)
        if len(main_text) < 50 and text_len > 300:
            return True, f"empty_main:main_tlen={len(main_text)}"

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


def _extract_images(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    提取页面图片 URL，按优先级排序：
      1. og:image（封面图，最可靠）
      2. 正文内图片（<main>/<article> 里的 <img>，过滤噪音）

    过滤规则：
      - 跳过尺寸 < 100px 的图（icon、追踪像素）
      - 跳过 src 含 icon/logo/avatar/pixel/sprite/badge 的图
      - 只取 http/https
    """
    _NOISE_KEYWORDS = ("icon", "logo", "avatar", "pixel", "sprite", "badge", "tracking", "placeholder", "blank", "spacer")

    def _is_noise(src: str) -> bool:
        src_lower = src.lower()
        return any(kw in src_lower for kw in _NOISE_KEYWORDS)

    def _is_too_small(tag) -> bool:
        for attr in ("width", "height"):
            val = tag.get(attr, "")
            try:
                if int(str(val).replace("px", "")) < 100:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    def _to_absolute(src: str) -> Optional[str]:
        if not src or src.startswith("data:"):
            return None
        absolute = urljoin(base_url, src)
        if not absolute.startswith(("http://", "https://")):
            return None
        return absolute

    seen: set[str] = set()
    images: list[str] = []

    def _add(url: str) -> None:
        if url and url not in seen:
            seen.add(url)
            images.append(url)

    # 1. og:image（最优先）
    og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if og:
        src = og.get("content", "").strip()
        abs_url = _to_absolute(src)
        if abs_url:
            _add(abs_url)

    # 2. 正文区域图片
    container = soup.find("article") or soup.find("main")
    if container:
        for img in container.find_all("img", src=True):
            src = img["src"].strip()
            if _is_noise(src) or _is_too_small(img):
                continue
            abs_url = _to_absolute(src)
            if abs_url:
                _add(abs_url)

    return images


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
    clean: bool = False,
    images: bool = False,
    rss: bool = False,
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

        # ── RSS / Atom 自动检测 ──────────────────────────────────────────
        #    rss=True 强制解析；或自动检测到 feed 格式时也走结构化路径
        if rss or _is_feed(response):
            feed_items = _parse_feed(response.text, str(response.url))
            if feed_items:
                return ExtractResult(
                    url=str(response.url),
                    level=1,
                    status_code=response.status_code,
                    title=f"RSS Feed ({len(feed_items)} items)",
                    text=json.dumps(feed_items, ensure_ascii=False, indent=2),
                    links=[item["link"] for item in feed_items if item.get("link")],
                    elapsed_ms=round(elapsed, 1),
                )

        # 解析内容
        soup = BeautifulSoup(response.text, "html.parser")
        title = _extract_title(soup)
        links = _extract_links(soup, str(response.url))
        imgs = _extract_images(soup, str(response.url)) if images else []

        # 尝试从 HTML 中提取嵌入 state（Next.js SSR 等）
        # 成功则直接返回结构化数据，无需浏览器，速度同 Level 1
        from rolling_reader.extractor.state import try_extract_state_from_html, state_to_text
        state_var, state_data = try_extract_state_from_html(response.text)
        if state_data is not None:
            return ExtractResult(
                url=str(response.url),
                level=1,
                status_code=response.status_code,
                title=title,
                text=state_to_text(state_var, state_data),
                links=links,
                images=imgs,
                elapsed_ms=round(elapsed, 1),
                state_var=state_var,
            )

        # --clean 模式：用 trafilatura 替换 BeautifulSoup 文本提取
        if clean:
            from rolling_reader.extractor.clean import clean_extract
            cleaned = clean_extract(response.text, url=str(response.url))
            text = cleaned if cleaned else _extract_text(soup)
        else:
            text = _extract_text(soup)

        return ExtractResult(
            url=str(response.url),
            level=1,
            status_code=response.status_code,
            title=title,
            text=text,
            links=links,
            images=imgs,
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
