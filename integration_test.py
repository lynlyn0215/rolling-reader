"""
integration_test.py
====================
ScrapeKit 端到端集成测试

验证整条链路：dispatcher → extractor → cache
覆盖：静态页、SPA、Cache 命中、内容质量、错误处理

运行：
    python integration_test.py
    python integration_test.py --no-chrome   # 只测 Level 1
    python integration_test.py --verbose
"""

from __future__ import annotations

import asyncio
import argparse
import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 测试用例定义
# ---------------------------------------------------------------------------

@dataclass
class IntegrationCase:
    url: str
    expected_level: int          # 期望最终使用的层级（1 / 2）
    min_title_len: int = 3       # title 至少这么长
    min_text_len: int = 100      # text 至少这么长
    min_links: int = 1           # 至少有这么多链接
    requires_chrome: bool = False
    notes: str = ""


CASES: list[IntegrationCase] = [
    # ── Level 1 静态页 ───────────────────────────────────────────────────
    IntegrationCase(
        "https://example.com", 1,
        min_text_len=50, min_links=1,
        notes="最简静态页",
    ),
    IntegrationCase(
        "https://news.ycombinator.com/", 1,
        min_title_len=5, min_text_len=500, min_links=50,
        notes="HackerNews SSR",
    ),
    IntegrationCase(
        "https://news.ycombinator.com/item?id=39000000", 1,
        min_text_len=100, min_links=5,
        notes="HackerNews 帖子",
    ),
    IntegrationCase(
        "https://arxiv.org/abs/1706.03762", 1,
        min_title_len=10, min_text_len=200, min_links=10,
        notes="arXiv 论文页",
    ),
    IntegrationCase(
        "https://docs.python.org/3/library/asyncio.html", 1,
        min_title_len=5, min_text_len=500, min_links=20,
        notes="Python 官方文档",
    ),
    IntegrationCase(
        "https://httpbin.org/html", 1,
        min_text_len=50, min_links=0,
        notes="纯 HTML 测试页",
    ),
    IntegrationCase(
        "https://text.npr.org/", 1,
        min_text_len=100, min_links=5,
        notes="NPR 纯文本版",
    ),
    IntegrationCase(
        "https://old.reddit.com/r/python", 1,
        min_text_len=200, min_links=20,
        notes="Reddit 旧版 SSR",
    ),
    IntegrationCase(
        "https://lobste.rs/", 1,
        min_text_len=200, min_links=20,
        notes="Lobsters SSR",
    ),
    IntegrationCase(
        "https://www.gutenberg.org/", 1,
        min_text_len=200, min_links=10,
        notes="古腾堡计划",
    ),

    # ── Level 2 SPA（需要 Chrome）────────────────────────────────────────
    IntegrationCase(
        "https://www.instagram.com/", 2,
        min_title_len=5, min_text_len=50,
        requires_chrome=True,
        notes="Instagram SPA",
    ),
    IntegrationCase(
        "https://www.youtube.com/", 2,
        min_title_len=5, min_text_len=50,
        requires_chrome=True,
        notes="YouTube SPA",
    ),
    IntegrationCase(
        "https://notion.so/", 2,
        min_title_len=3, min_text_len=50,
        requires_chrome=True,
        notes="Notion SPA",
    ),
    IntegrationCase(
        "https://www.figma.com/", 2,
        min_title_len=3, min_text_len=50,
        requires_chrome=True,
        notes="Figma SPA",
    ),
    IntegrationCase(
        "https://www.tiktok.com/", 2,
        min_title_len=3, min_text_len=10,
        requires_chrome=True,
        notes="TikTok SPA",
    ),

    # ── 需要升级的混合页（Level 1 失败 → Level 2）────────────────────────
    IntegrationCase(
        "https://www.producthunt.com/", 2,
        min_title_len=5, min_text_len=100,
        requires_chrome=True,
        notes="403 → 升级到 Level 2",
    ),
    IntegrationCase(
        "https://codepen.io/trending", 2,
        min_title_len=3, min_text_len=10,
        requires_chrome=True,
        notes="403 → 升级到 Level 2",
    ),
    IntegrationCase(
        "https://replit.com/", 2,
        min_title_len=3, min_text_len=50,
        requires_chrome=True,
        notes="SPA → Level 2",
    ),
    IntegrationCase(
        "https://vercel.com/", 2,
        min_title_len=3, min_text_len=50,
        requires_chrome=True,
        notes="Next.js → Level 2",
    ),
    IntegrationCase(
        "https://dribbble.com/", 2,
        min_title_len=3, min_text_len=50,
        requires_chrome=True,
        notes="SPA → Level 2",
    ),
]


# ---------------------------------------------------------------------------
# 内容质量检查
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    url: str
    notes: str
    expected_level: int
    actual_level: Optional[int] = None
    level_ok: Optional[bool] = None
    title: str = ""
    title_ok: Optional[bool] = None
    text_len: int = 0
    text_ok: Optional[bool] = None
    links_count: int = 0
    links_ok: Optional[bool] = None
    elapsed_ms: float = 0
    error: Optional[str] = None
    skipped: bool = False

    @property
    def passed(self) -> bool:
        if self.skipped or self.error:
            return False
        return all([self.level_ok, self.title_ok, self.text_ok, self.links_ok])


def check_result(result, case: IntegrationCase) -> CaseResult:
    from scrapekit.models import ExtractResult
    cr = CaseResult(url=case.url, notes=case.notes, expected_level=case.expected_level)
    cr.actual_level = result.level
    cr.level_ok     = result.level == case.expected_level
    cr.title        = result.title
    cr.title_ok     = len(result.title) >= case.min_title_len
    cr.text_len     = len(result.text)
    cr.text_ok      = len(result.text) >= case.min_text_len
    cr.links_count  = len(result.links)
    cr.links_ok     = len(result.links) >= case.min_links
    cr.elapsed_ms   = result.elapsed_ms
    return cr


# ---------------------------------------------------------------------------
# 测试执行
# ---------------------------------------------------------------------------

async def run_case(
    case: IntegrationCase,
    chrome_available: bool,
    verbose: bool,
) -> CaseResult:
    from scrapekit.dispatcher import dispatch
    from scrapekit.models import ExtractionError

    if case.requires_chrome and not chrome_available:
        cr = CaseResult(url=case.url, notes=case.notes,
                        expected_level=case.expected_level)
        cr.skipped = True
        if verbose:
            print(f"  SKIP  {case.url[:55]:<55}  (Chrome not available)")
        return cr

    try:
        result = await dispatch(case.url, verbose=False, use_cache=False)
        cr = check_result(result, case)
        if verbose:
            ok = "✓" if cr.passed else "✗"
            issues = []
            if not cr.level_ok:
                issues.append(f"level={cr.actual_level}≠{cr.expected_level}")
            if not cr.title_ok:
                issues.append(f"title_len={len(cr.title)}")
            if not cr.text_ok:
                issues.append(f"text_len={cr.text_len}")
            if not cr.links_ok:
                issues.append(f"links={cr.links_count}")
            issue_str = "  " + ", ".join(issues) if issues else ""
            print(
                f"  {ok}  L{cr.actual_level}  {case.url[:50]:<50}  "
                f"{cr.elapsed_ms:6.0f}ms  {case.notes}{issue_str}"
            )
        return cr

    except ExtractionError as e:
        cr = CaseResult(url=case.url, notes=case.notes,
                        expected_level=case.expected_level)
        cr.error = str(e)
        if verbose:
            print(f"  ERR  {case.url[:55]:<55}  {e}")
        return cr
    except Exception as e:
        cr = CaseResult(url=case.url, notes=case.notes,
                        expected_level=case.expected_level)
        cr.error = f"unexpected: {e}"
        if verbose:
            print(f"  ERR  {case.url[:55]:<55}  {e}")
        return cr


async def run_all(chrome_available: bool, verbose: bool) -> list[CaseResult]:
    print(f"\n{'='*72}")
    print(f"  ScrapeKit 集成测试")
    print(f"  URL 总数: {len(CASES)}  |  Chrome: {'✓' if chrome_available else '✗ (Level 2 用例跳过)'}")
    print(f"{'='*72}\n")

    if verbose:
        print(f"  {'状态':4} {'L':2}  {'URL':50}  {'耗时':>6}  备注")
        print(f"  {'-'*100}")

    # 逐一执行（不并发，避免 Chrome 多标签冲突）
    results = []
    for case in CASES:
        r = await run_case(case, chrome_available, verbose)
        results.append(r)

    return results


# ---------------------------------------------------------------------------
# 第二轮：Cache 命中测试
# ---------------------------------------------------------------------------

async def run_cache_test(verbose: bool) -> None:
    from scrapekit.dispatcher import dispatch
    from scrapekit.cache import profile as cache

    url = "https://news.ycombinator.com/"
    print(f"\n{'='*72}")
    print("  Cache 命中测试")
    print(f"{'='*72}\n")

    # 清除旧 cache
    cache.invalidate(url)

    # 第一次：探索
    t0 = time.perf_counter()
    r1 = await dispatch(url, verbose=False, use_cache=True)
    t1 = (time.perf_counter() - t0) * 1000

    # 第二次：应命中 cache
    t0 = time.perf_counter()
    r2 = await dispatch(url, verbose=False, use_cache=True)
    t2 = (time.perf_counter() - t0) * 1000

    p = cache.load(url)

    print(f"  第一次（探索）: level={r1.level}  {t1:.0f}ms")
    print(f"  第二次（cache）: level={r2.level}  {t2:.0f}ms")
    print(f"  cache 内容: level={p.get('preferred_level')}  "
          f"success_count={p.get('success_count')}  "
          f"domain={p.get('domain')}")

    if r1.level == r2.level and p.get("success_count", 0) >= 2:
        print("  ✓ Cache 命中正常")
    else:
        print("  ✗ Cache 行为异常")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def print_report(results: list[CaseResult]) -> None:
    tested  = [r for r in results if not r.skipped and not r.error]
    skipped = [r for r in results if r.skipped]
    errors  = [r for r in results if r.error]
    passed  = [r for r in tested if r.passed]
    failed  = [r for r in tested if not r.passed]

    total = len(tested)
    acc = len(passed) / total * 100 if total else 0

    print(f"\n{'='*72}")
    print(f"  集成测试结果")
    print(f"{'='*72}")
    print(f"  总计: {len(results)}  已测: {total}  跳过: {len(skipped)}  报错: {len(errors)}")
    print(f"  通过: {len(passed)}  失败: {len(failed)}")
    print(f"  通过率: {acc:.1f}%")

    # 按层级分组
    l1 = [r for r in tested if r.expected_level == 1]
    l2 = [r for r in tested if r.expected_level == 2]
    l1_ok = [r for r in l1 if r.passed]
    l2_ok = [r for r in l2 if r.passed]
    print(f"\n  Level 1 静态页: {len(l1_ok)}/{len(l1)} 通过")
    print(f"  Level 2 SPA:    {len(l2_ok)}/{len(l2)} 通过")

    if failed:
        print(f"\n  失败详情:")
        for r in failed:
            issues = []
            if not r.level_ok:
                issues.append(f"level={r.actual_level}(期望{r.expected_level})")
            if not r.title_ok:
                issues.append(f"title太短({len(r.title)})")
            if not r.text_ok:
                issues.append(f"text太短({r.text_len})")
            if not r.links_ok:
                issues.append(f"links不足({r.links_count})")
            print(f"    ✗ {r.url}")
            print(f"      {', '.join(issues)}")
            print(f"      title={r.title!r}")

    if errors:
        print(f"\n  错误:")
        for r in errors:
            print(f"    ✗ {r.url}")
            print(f"      {r.error}")

    print(f"\n{'='*72}")
    if acc >= 90:
        print(f"  ✅ 集成测试通过 ({acc:.1f}%)")
    elif acc >= 70:
        print(f"  ⚠️  部分通过 ({acc:.1f}%)，有问题需要修复")
    else:
        print(f"  ❌ 集成测试不通过 ({acc:.1f}%)")
    print(f"{'='*72}\n")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def main(no_chrome: bool, verbose: bool) -> None:
    from scrapekit.extractor.cdp import is_chrome_available

    chrome_available = False
    if not no_chrome:
        chrome_available = await is_chrome_available()
        if not chrome_available:
            print("⚠  Chrome 未运行，Level 2 用例将跳过")
            print("   启动命令: chrome --remote-debugging-port=9222\n")

    results = await run_all(chrome_available, verbose)
    print_report(results)
    await run_cache_test(verbose)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-chrome", action="store_true",
                        help="跳过所有需要 Chrome 的用例")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="逐条打印结果")
    args = parser.parse_args()

    asyncio.run(main(args.no_chrome, args.verbose))
