"""
rolling_reader/dispatcher.py
========================
核心调度器：按策略阶梯自动升级。

Level 1 → Level 2 → Level 3（v0.1 只支持到 Level 2）

升级条件：
  - Level 1 抛出 NeedsBrowserError → 升级到 Level 2
  - Level 1 抛出 ExtractionError  → 升级到 Level 2（网络失败也值得用浏览器重试）
  - --force-level N              → 跳过前面的层级

未来扩展：
  - Level 2 → Level 3：检测到 __PRELOADED_STATE__ 等 JS state 变量后升级
  - Profile Cache：命中缓存时直接跳到已知层级
"""

from __future__ import annotations

import asyncio
from typing import Optional

from rolling_reader.models import ExtractResult, NeedsBrowserError, ExtractionError
from rolling_reader.extractor import http_extract, cdp_extract, is_chrome_available
from rolling_reader.cache import profile as profile_cache


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def dispatch(
    url: str,
    *,
    force_level: Optional[int] = None,
    cdp_endpoint: str = "http://localhost:9222",
    http_timeout: float = 15.0,
    page_timeout: float = 30.0,
    verbose: bool = False,
    use_cache: bool = True,
) -> ExtractResult:
    """
    自动选择最优抓取策略并执行。

    Args:
        url:          目标 URL
        force_level:  强制使用指定层级（1/2/3），跳过自动判断
        cdp_endpoint: Chrome 调试端点
        http_timeout: Level 1 HTTP 超时（秒）
        page_timeout: Level 2/3 页面加载超时（秒）
        verbose:      打印升级过程
        use_cache:    是否使用 Profile Cache

    Returns:
        ExtractResult

    Raises:
        ExtractionError: 所有层级均失败
    """
    def log(msg: str) -> None:
        if verbose:
            print(f"[rolling-reader] {msg}", flush=True)

    # ── 强制指定层级 ──────────────────────────────────────────────────────
    if force_level == 1:
        log("forced Level 1 (HTTP)")
        return await http_extract(url, timeout=http_timeout)

    if force_level in (2, 3):
        log(f"forced Level 2/3 (CDP)")
        return await _try_level2(url, cdp_endpoint, page_timeout, log)

    # ── Profile Cache：命中时直接跳到已知层级 ─────────────────────────────
    if use_cache:
        cached = profile_cache.load(url)
        if cached:
            preferred = cached.get("preferred_level", 1)
            log(f"cache hit → Level {preferred} for {cached.get('domain')}")
            if preferred == 1:
                try:
                    result = await http_extract(url, timeout=http_timeout)
                    profile_cache.save(url, result.level)
                    return result
                except Exception:
                    log("cache: Level 1 failed, invalidating and re-exploring")
                    profile_cache.invalidate(url)
            else:
                try:
                    result = await _try_level2(url, cdp_endpoint, page_timeout, log)
                    profile_cache.save(url, result.level,
                                       state_var=cached.get("state_var"))
                    return result
                except Exception:
                    log("cache: Level 2 failed, invalidating and re-exploring")
                    profile_cache.invalidate(url)

    # ── 自动升级探索 ──────────────────────────────────────────────────────

    # Level 1：HTTP 直取
    log(f"Level 1 → {url}")
    try:
        result = await http_extract(url, timeout=http_timeout)
        log(f"Level 1 succeeded ({result.elapsed_ms:.0f}ms)")
        if use_cache:
            profile_cache.save(url, result.level)
        return result

    except NeedsBrowserError as e:
        log(f"Level 1 → needs browser ({e.reason}), escalating to Level 2/3")

    except ExtractionError as e:
        log(f"Level 1 → error ({e.reason}), escalating to Level 2/3")

    # Level 2/3：CDP + 已有 Chrome（内部自动尝试 Level 3 state 提取）
    result = await _try_level2(url, cdp_endpoint, page_timeout, log)
    if use_cache:
        state_var = None
        if result.level == 3:
            from rolling_reader.extractor.state import KNOWN_STATE_VARS
            state_var = KNOWN_STATE_VARS[0]   # v0.1 固定
        profile_cache.save(url, result.level, state_var=state_var)
    return result


async def _try_level2(
    url: str,
    cdp_endpoint: str,
    page_timeout: float,
    log,
) -> ExtractResult:
    """尝试 Level 2，Chrome 不可用时给出清晰错误。"""
    from rolling_reader.extractor.cdp import ChromeNotRunningError

    log(f"Level 2 → {url}")

    # 提前探测 Chrome，给出更友好的错误
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
        )
        log(f"Level 2 succeeded ({result.elapsed_ms:.0f}ms)")
        return result

    except ChromeNotRunningError:
        raise
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(url, f"Level 2 unexpected error: {e}") from e
