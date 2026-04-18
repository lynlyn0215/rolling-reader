"""
rolling_reader/dispatcher.py
========================
核心调度器：按策略阶梯自动升级。

Level 1 → Level 2 → Level 3

升级条件：
  - Level 1 抛出 NeedsBrowserError → 升级到 Level 2
  - Level 1 抛出 ExtractionError  → 升级到 Level 2（网络失败也值得用浏览器重试）
  - --force-level N              → 跳过前面的层级

Profile Cache v0.2 行为：
  - TTL 从 last_success 起算 7 天（原 discovered_at 30 天）
  - 软失败：连续失败 < 3 次保留 cache，≥3 次才硬删
  - L2/3 reprobe：每成功 20 次，下次命中时悄悄试一次 L1，成功则自动降级
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
    clean: bool = False,
    images: bool = False,
    rss: bool = False,
    retries: int = 2,
    meta: bool = False,
    select: Optional[str] = None,
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
        return await http_extract(url, timeout=http_timeout, clean=clean, images=images, rss=rss, retries=retries, meta=meta, select=select)

    if force_level in (2, 3):
        log(f"forced Level 2/3 (CDP)")
        return await _browser_extract(url, cdp_endpoint, page_timeout, log, clean=clean, images=images, rss=rss)

    # ── Profile Cache：命中时直接跳到已知层级 ─────────────────────────────
    if use_cache:
        cached = profile_cache.load(url)
        if cached:
            preferred = cached.get("preferred_level", 1)
            reprobe_due = cached.get("reprobe_due", False)
            log(f"cache hit → Level {preferred} for {cached.get('domain')}"
                + (" [reprobe_due]" if reprobe_due else ""))

            if preferred == 1:
                try:
                    result = await http_extract(url, timeout=http_timeout, clean=clean, images=images, rss=rss, retries=retries, meta=meta)
                    profile_cache.save(url, result.level)
                    return result
                except (NeedsBrowserError, ExtractionError) as e:
                    hard = profile_cache.record_failure(url)
                    log(f"cache: Level 1 failed ({e.reason}), "
                        + ("hard-invalidated, re-exploring" if hard else f"soft-fail ({cached.get('failure_count', 0)+1}/{profile_cache.SOFT_FAIL_THRESHOLD}), re-exploring"))
                # 软失败后继续走自动升级逻辑（不 return）

            else:
                # L2/3 reprobe：悄悄试一次 L1，看站点是否已 SSR 化
                if reprobe_due and await is_chrome_available(cdp_endpoint):
                    log("reprobe: trying L1 to check if site now supports SSR")
                    try:
                        result = await http_extract(url, timeout=http_timeout, clean=clean, images=images, rss=rss, retries=retries, meta=meta)
                        import sys
                        print(f"rr: reprobe succeeded L1 for {cached.get('domain')}, downgrading cache", file=sys.stderr)
                        profile_cache.save(url, result.level)
                        return result
                    except (NeedsBrowserError, ExtractionError):
                        log("reprobe: L1 still fails, staying on L2/3")
                        # reprobe_due 在下次 save() 时会重置，继续走 L2

                try:
                    result = await _browser_extract(url, cdp_endpoint, page_timeout, log, clean=clean, images=images, rss=rss)
                    profile_cache.save(url, result.level, state_var=result.state_var)
                    return result
                except (NeedsBrowserError, ExtractionError) as e:
                    hard = profile_cache.record_failure(url)
                    log(f"cache: browser path failed ({e.reason}), "
                        + ("hard-invalidated" if hard else "soft-fail"))
                    if not hard:
                        raise  # 软失败：直接把错误抛出，不降级重探

    # ── 自动升级探索 ──────────────────────────────────────────────────────

    # Level 1：HTTP 直取
    log(f"Level 1 → {url}")
    try:
        result = await http_extract(url, timeout=http_timeout, clean=clean, images=images, rss=rss, retries=retries, meta=meta)
        log(f"Level 1 succeeded ({result.elapsed_ms:.0f}ms)")
        if use_cache:
            profile_cache.save(url, result.level)
        return result

    except NeedsBrowserError as e:
        import sys
        print(f"rr: → L1 failed ({e.reason}), escalating to L2", file=sys.stderr)
        log(f"Level 1 → needs browser ({e.reason}), escalating to Level 2/3")

    except ExtractionError as e:
        import sys
        print(f"rr: → L1 error ({e.reason}), escalating to L2", file=sys.stderr)
        log(f"Level 1 → error ({e.reason}), escalating to Level 2/3")

    # Level 2/3：CDP + 已有 Chrome（内部自动尝试 Level 3 state 提取）
    result = await _browser_extract(url, cdp_endpoint, page_timeout, log, clean=clean, images=images, rss=rss)
    if use_cache:
        profile_cache.save(url, result.level, state_var=result.state_var)
    return result


async def _browser_extract(
    url: str,
    cdp_endpoint: str,
    page_timeout: float,
    log,
    *,
    clean: bool = False,
    images: bool = False,
    rss: bool = False,
) -> ExtractResult:
    """CDP 路径：尝试 Level 2 DOM 提取，内部自动升级到 Level 3 JS State。"""
    from rolling_reader.extractor.cdp import ChromeNotRunningError

    log(f"browser path → {url}")

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
            clean=clean,
            images=images,
        )
        log(f"browser path succeeded L{result.level} ({result.elapsed_ms:.0f}ms)")
        return result

    except ChromeNotRunningError:
        raise
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(url, f"Level 2 unexpected error: {e}") from e
