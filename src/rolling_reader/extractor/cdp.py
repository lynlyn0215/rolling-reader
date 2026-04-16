"""
rolling_reader/extractor/cdp.py
===========================
Level 2 — CDP + 已有 Chrome Session（Playwright connect_over_cdp）

核心优势：
  复用用户已经登录的 Chrome，无需重新认证、无需存储凭据。
  Chrome 需提前以 --remote-debugging-port=9222 启动（见 README）。

流程：
  1. 连接到 localhost:9222
  2. 取已有 context（继承登录态 / cookies）
  3. 新开一个标签页，导航到目标 URL
  4. 等待页面加载（domcontentloaded + networkidle）
  5. 提取 HTML，复用 Level 1 的 BeautifulSoup 逻辑
  6. 关闭标签页，不污染 Chrome 会话

错误处理：
  - Chrome 未启动 → ChromeNotRunningError（清晰提示）
  - 页面加载超时 → ExtractionError
  - 其他 → ExtractionError
"""

from __future__ import annotations

import time
from typing import Optional

from bs4 import BeautifulSoup

from rolling_reader.models import ExtractResult, ExtractionError
from rolling_reader.extractor.http import (
    _extract_title,
    _extract_text,
    _extract_links,
)

# CDP 端口（可通过环境变量覆盖）
CDP_ENDPOINT = "http://localhost:9222"

# 等待策略
WAIT_UNTIL = "domcontentloaded"   # 第一阶段：DOM 就绪
NETWORK_IDLE_TIMEOUT = 5_000      # ms，等 networkidle 的最长时间（不强制）


# ---------------------------------------------------------------------------
# 专属异常
# ---------------------------------------------------------------------------

class ChromeNotRunningError(ExtractionError):
    """
    Chrome 未以 --remote-debugging-port=9222 运行时抛出。
    提示用户如何启动 Chrome。
    """
    def __init__(self, endpoint: str = CDP_ENDPOINT):
        super().__init__(
            url="",
            reason=(
                f"Cannot connect to Chrome at {endpoint}. "
                "Start Chrome with: "
                "chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug"
            ),
        )


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

async def extract(
    url: str,
    *,
    cdp_endpoint: str = CDP_ENDPOINT,
    page_timeout: float = 30.0,
    wait_networkidle: bool = True,
    clean: bool = False,
) -> ExtractResult:
    """
    Level 2 CDP 抓取。

    Args:
        url:               目标 URL
        cdp_endpoint:      Chrome DevTools 端点，默认 http://localhost:9222
        page_timeout:      页面导航超时（秒）
        wait_networkidle:  是否等待 networkidle（SPA 内容渲染完毕）

    Returns:
        ExtractResult（level=2）

    Raises:
        ChromeNotRunningError: Chrome 未启动或未开启远程调试
        ExtractionError:       页面加载或提取失败
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError as e:
        raise ExtractionError(
            url,
            "playwright is not installed. Run: pip install rolling-reader\n"
            "  (playwright is a dependency — if you see this, your install may be incomplete)"
        ) from e

    t0 = time.perf_counter()

    async with async_playwright() as pw:
        # ── 1. 连接已有 Chrome ────────────────────────────────────────────
        try:
            browser = await pw.chromium.connect_over_cdp(
                cdp_endpoint,
                timeout=5_000,   # 连接超时 5s，快速失败
            )
        except Exception as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ("connection refused", "connect", "econnrefused", "failed to connect")):
                raise ChromeNotRunningError(cdp_endpoint) from e
            raise ExtractionError(url, f"cdp connect error: {e}") from e

        # ── 2. 取已有 context（继承登录态）────────────────────────────────
        if browser.contexts:
            context = browser.contexts[0]
        else:
            # 极少数情况：Chrome 连上了但没有 context（无窗口模式）
            context = await browser.new_context()

        # ── 3. 开新标签页 ─────────────────────────────────────────────────
        page = await context.new_page()

        try:
            # ── 4. 导航 ───────────────────────────────────────────────────
            try:
                await page.goto(
                    url,
                    wait_until=WAIT_UNTIL,
                    timeout=page_timeout * 1000,
                )
            except PlaywrightTimeout as e:
                raise ExtractionError(url, f"page load timeout: {e}") from e
            except Exception as e:
                raise ExtractionError(url, f"navigation error: {e}") from e

            # ── 5. 等待 networkidle（可选，给 SPA 时间完成渲染）───────────
            #    文档说 networkidle 不推荐用于 CI 测试（flaky），
            #    但对爬虫来说是正确选择：等 JS 执行完毕再抓 DOM
            if wait_networkidle:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=NETWORK_IDLE_TIMEOUT,
                    )
                except PlaywrightTimeout:
                    # networkidle 超时不致命，继续提取已有内容
                    pass

            # ── 6. 尝试 Level 3 JS State 提取（在关闭标签页之前）────────────
            from rolling_reader.extractor.state import try_extract_state, state_to_text
            state_var, state_data = await try_extract_state(page)

            # ── 7. 提取 HTML（Level 2 DOM 路径）──────────────────────────────
            html = await page.content()
            final_url = page.url

        finally:
            # 始终关闭标签页，不污染 Chrome
            await page.close()

    elapsed = (time.perf_counter() - t0) * 1000

    # ── 8. Level 3：有 JS state → 直接返回结构化 JSON ─────────────────────
    if state_data is not None:
        soup = BeautifulSoup(html, "html.parser")
        return ExtractResult(
            url=final_url,
            level=3,
            status_code=200,
            title=_extract_title(soup),
            text=state_to_text(state_var, state_data),
            links=_extract_links(soup, final_url),
            elapsed_ms=round(elapsed, 1),
        )

    # ── 9. Level 2：回退到 DOM 提取 ───────────────────────────────────────
    soup = BeautifulSoup(html, "html.parser")
    if clean:
        from rolling_reader.extractor.clean import clean_extract
        cleaned = clean_extract(html, url=final_url)
        text = cleaned if cleaned else _extract_text(BeautifulSoup(html, "html.parser"))
    else:
        text = _extract_text(BeautifulSoup(html, "html.parser"))
    return ExtractResult(
        url=final_url,
        level=2,
        status_code=200,
        title=_extract_title(soup),
        text=text,
        links=_extract_links(soup, final_url),
        elapsed_ms=round(elapsed, 1),
    )


# ---------------------------------------------------------------------------
# 工具：检查 Chrome 是否可连接
# ---------------------------------------------------------------------------

async def is_chrome_available(cdp_endpoint: str = CDP_ENDPOINT) -> bool:
    """快速探测 Chrome 是否在指定端口运行。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{cdp_endpoint}/json/version")
            return resp.status_code == 200
    except Exception:
        return False
