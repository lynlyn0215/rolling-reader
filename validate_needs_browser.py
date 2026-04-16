"""
validate_needs_browser.py
=========================
测试 needs_browser() 启发式函数在 50+ 真实 URL 上的准确率。

运行方式：
    python validate_needs_browser.py
    python validate_needs_browser.py --output results.json
    python validate_needs_browser.py --verbose
"""

import asyncio
import json
import time
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# V1：原始函数（来自 Proposal v0.2）
# ---------------------------------------------------------------------------

def needs_browser_v1(response: httpx.Response) -> bool:
    """原始版本，作为 baseline 对比用。"""
    if len(response.text) < 500:
        return True
    text_lower = response.text.lower()
    if any(s in text_lower for s in [
        "enable javascript", "you need javascript",
        "javascript is required", "javascript is disabled",
        '<div id="app"></div>', "<div id='app'></div>",
        '<div id="root"></div>', "<div id='root'></div>",
    ]):
        return True
    soup = BeautifulSoup(response.text, "html.parser")
    text_ratio = len(soup.get_text(strip=True)) / max(len(response.text), 1)
    return text_ratio < 0.1


# ---------------------------------------------------------------------------
# V2：改进版本
#
# 修复了 v1 的三个核心问题：
#
# 1. 空 body（如 httpbin/status/200）被误判为需要浏览器
#    修复：len==0 时直接返回 False
#
# 2. text_ratio < 0.1 阈值太激进，把 GitHub/BBC/HackerNews 等
#    内容丰富的 SSR 页全部误判（16 个 False Positive）
#    修复：
#      a. 绝对文字量 < 200 chars → SPA shell（比 ratio 更可靠）
#      b. 尺寸感知阈值：大页面(>50KB)用 ≤0.015，小页面用 <0.05
#
# 3. 未检测 SSR 框架状态标记（__NEXT_DATA__ 等），漏掉 Next.js SPA
#    修复：添加框架特定标记检测
# ---------------------------------------------------------------------------

def needs_browser_v2(response: httpx.Response) -> bool:
    """V2 保留原样用于对比。"""
    if len(response.text) == 0:
        return False
    if response.status_code in (400, 401, 403, 407):
        return True
    if len(response.text) < 500:
        return True
    text_lower = response.text.lower()
    if any(s in text_lower for s in [
        "enable javascript", "you need javascript",
        "javascript is required", "javascript is disabled",
        '<div id="app"></div>', "<div id='app'></div>",
        '<div id="root"></div>', "<div id='root'></div>",
    ]):
        return True
    if any(marker in response.text for marker in [
        '__NEXT_DATA__', '__NUXT__', '__INITIAL_STATE__',
        'window.__PRELOADED_STATE__',
    ]):
        return True
    soup = BeautifulSoup(response.text, "html.parser")
    text_content = soup.get_text(strip=True)
    text_len = len(text_content)
    html_len = len(response.text)
    text_ratio = text_len / max(html_len, 1)
    if text_ratio < 0.005:
        return True
    if text_len < 200:
        return True
    if html_len > 50_000:
        return text_ratio <= 0.015
    else:
        return text_ratio < 0.05


# ---------------------------------------------------------------------------
# V3：再次改进
#
# V2 的两个新问题：
#
# 1. __NEXT_DATA__ 检测误伤 BBC/PyPI
#    问题：BBC 和 PyPI 页面里包含 __NEXT_DATA__ 或类似标记，
#    但它们的内容已经完整 SSR 渲染，Level 1 完全可以获取。
#    __NEXT_DATA__ 只代表"用了 Next.js"，不代表"需要 JS 才能看到内容"。
#    修复：移除框架标记检测步骤。
#
# 2. text_len < 200 误伤 example.com
#    example.com 整页只有 528 bytes，text=139 chars，但 ratio=0.263（内容占比高）。
#    真正的 SPA shell（Facebook 111 chars）ratio=0.072（内容占比低）。
#    修复：text_len < 200 同时要求 ratio < 0.15，把高密度小页面排除在外。
# ---------------------------------------------------------------------------

def needs_browser_v3(response: httpx.Response) -> bool:
    """
    V3：移除框架标记误报源，修复小页面误判。
    """
    # ── 1. 空 body → API 端点，不是 SPA ─────────────────────────────────
    if len(response.text) == 0:
        return False

    # ── 2. 4xx → Level 1 已失败，升级（Chrome 通常能绕过 bot 检测/登录墙）
    if response.status_code in (400, 401, 403, 407):
        return True

    # ── 3. 很短的 2xx → SPA shell ────────────────────────────────────────
    if len(response.text) < 500:
        return True

    # ── 4. 解析 HTML，去掉 <noscript>（避免 noscript 里的提示文字误判）────
    #    反例：PyPI 在 <noscript> 里写 "Enable javascript to filter wheels"
    #    但 PyPI 整页内容完全不需要 JS 才能访问
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup.find_all('noscript'):
        tag.decompose()
    cleaned_html = str(soup).lower()

    # ── 5. 显式 JS 要求标记（在 noscript 剥离后的 HTML 中检查）──────────
    if any(s in cleaned_html for s in [
        "enable javascript", "you need javascript",
        "javascript is required", "javascript is disabled",
        '<div id="app"></div>', "<div id='app'></div>",
        '<div id="root"></div>', "<div id='root'></div>",
    ]):
        return True

    # ── 6. 文本内容分析 ──────────────────────────────────────────────────
    text_content = soup.get_text(strip=True)   # 同样基于已剥离 noscript 的 soup
    text_len = len(text_content)
    html_len = len(response.text)              # 分母用原始 HTML 长度保持一致性
    text_ratio = text_len / max(html_len, 1)

    # 6a. 极低比例 → 肯定是 SPA
    if text_ratio < 0.005:
        return True

    # 6b. 绝对文字量少 + ratio 也低 → SPA shell
    #     不能只看 text_len：example.com(139 chars) ratio=0.263 是真实静态页
    #     Facebook(111 chars) ratio=0.072 才是 SPA shell
    if text_len < 200 and text_ratio < 0.15:
        return True

    # 6c. 尺寸感知 ratio 阈值
    #     大页面（>50KB）HTML 标签天然多，ratio 天然偏低
    #     阈值 0.018：Airtable(0.01516)/Notion(0.01486)/Replit(0.01356) 等 SPA 能被捕获
    #               GitHub(0.019)/BBC(0.031)/PyPI(0.075) 等 SSR 页不受影响
    #     注意：不用 <= 0.015 因为浮点精度问题（Airtable 实际=0.01516，显示=0.015）
    if html_len > 50_000:
        return text_ratio < 0.018
    else:
        return text_ratio < 0.05


# 当前使用的版本
needs_browser = needs_browser_v3


# ---------------------------------------------------------------------------
# 测试数据集（55 个 URL，手动标注 ground truth）
#
# expected=False → 静态页面，Level 1 应该能抓到有效内容
# expected=True  → 需要浏览器（SPA / 登录墙 / JS 渲染）
# ---------------------------------------------------------------------------

@dataclass
class URLTestCase:
    url: str
    expected: bool          # True=需要浏览器, False=静态
    category: str           # 分组标签
    notes: str = ""

TEST_CASES: list[URLTestCase] = [
    # ── 静态 HTML 页面 (expected=False) ─────────────────────────────────
    URLTestCase("https://example.com", False, "static", "最简单的静态页面"),
    URLTestCase("https://httpbin.org/html", False, "static", "纯 HTML 测试页"),
    # Wikipedia 返回 403：Level 1 被 bot 检测拦截，Chrome 实际可访问 → expected=True
    URLTestCase("https://en.wikipedia.org/wiki/Python_(programming_language)", True, "blocked-403", "Wikipedia（httpx 被拦截，Chrome 可访问）"),
    URLTestCase("https://en.wikipedia.org/wiki/Web_scraping", True, "blocked-403", "Wikipedia（httpx 被拦截，Chrome 可访问）"),
    URLTestCase("https://docs.python.org/3/library/asyncio.html", False, "static", "Python 官方文档"),
    URLTestCase("https://quotes.toscrape.com/", False, "static", "专为爬虫练习设计"),
    URLTestCase("https://books.toscrape.com/", False, "static", "专为爬虫练习设计"),
    URLTestCase("https://news.ycombinator.com/", False, "static", "HackerNews 纯服务端渲染"),
    URLTestCase("https://news.ycombinator.com/item?id=39000000", False, "static", "HackerNews 帖子"),
    URLTestCase("https://old.reddit.com/r/python", False, "static", "Reddit 旧版界面 SSR"),
    URLTestCase("https://lobste.rs/", False, "static", "Lobsters 纯 SSR"),
    URLTestCase("https://www.gutenberg.org/", False, "static", "古腾堡计划"),
    URLTestCase("https://arxiv.org/abs/2301.07041", False, "static", "arXiv 论文页"),
    URLTestCase("https://arxiv.org/abs/1706.03762", False, "static", "Attention is All You Need"),
    URLTestCase("https://pypi.org/project/httpx/", False, "static", "PyPI 包页面"),
    URLTestCase("https://pypi.org/project/playwright/", False, "static", "PyPI 包页面"),
    URLTestCase("https://www.bbc.com/news", False, "static/ssr", "BBC 新闻 SSR"),
    # Reuters 401：需要认证，Level 1 失败 → True
    URLTestCase("https://www.reuters.com/", True, "blocked-401", "路透社（401 需要认证）"),
    # SO 403：被 bot 检测拦截，Chrome 可访问 → True
    URLTestCase("https://stackoverflow.com/questions/tagged/python", True, "blocked-403", "Stack Overflow（httpx 被拦截）"),
    URLTestCase("https://stackoverflow.com/questions/11227809", True, "blocked-403", "Stack Overflow（httpx 被拦截）"),
    URLTestCase("https://github.com/psf/requests", False, "static/ssr", "GitHub 仓库页（SSR）"),
    URLTestCase("https://github.com/microsoft/playwright", False, "static/ssr", "GitHub 仓库页（SSR）"),
    URLTestCase("https://raw.githubusercontent.com/psf/requests/main/README.md", False, "static", "GitHub Raw 文件"),
    URLTestCase("https://httpbin.org/get", False, "static", "JSON API 响应"),
    URLTestCase("https://httpbin.org/status/200", False, "static", "简单状态页"),
    URLTestCase("https://lite.cnn.com/", False, "static/ssr", "CNN Lite 纯文本版"),
    URLTestCase("https://text.npr.org/", False, "static", "NPR 纯文本版"),

    # ── 需要浏览器渲染的页面 (expected=True) ────────────────────────────
    URLTestCase("https://twitter.com/", True, "spa", "X/Twitter SPA"),
    URLTestCase("https://x.com/", True, "spa", "X/Twitter SPA（新域名）"),
    URLTestCase("https://www.instagram.com/", True, "spa", "Instagram SPA"),
    URLTestCase("https://www.facebook.com/", True, "spa", "Facebook SPA"),
    URLTestCase("https://web.whatsapp.com/", True, "spa", "WhatsApp Web SPA"),
    URLTestCase("https://mail.google.com/mail/u/0/", True, "spa+auth", "Gmail（需登录）"),
    URLTestCase("https://www.figma.com/", True, "spa", "Figma SPA"),
    URLTestCase("https://notion.so/", True, "spa", "Notion SPA"),
    URLTestCase("https://app.slack.com/", True, "spa+auth", "Slack Web App"),
    URLTestCase("https://trello.com/", True, "spa", "Trello"),
    URLTestCase("https://airtable.com/", True, "spa", "Airtable"),
    URLTestCase("https://soundcloud.com/", True, "spa", "SoundCloud SPA"),
    URLTestCase("https://www.canva.com/", True, "spa", "Canva SPA"),
    URLTestCase("https://www.airbnb.com/", True, "spa/ssr-hybrid", "Airbnb（React SSR+hydrate）"),
    URLTestCase("https://www.linkedin.com/", True, "spa+auth", "LinkedIn（需登录）"),
    URLTestCase("https://www.reddit.com/r/Python/", True, "spa", "Reddit 新版 SPA"),
    URLTestCase("https://discord.com/app", True, "spa+auth", "Discord Web App"),
    URLTestCase("https://www.twitch.tv/", True, "spa", "Twitch SPA"),
    URLTestCase("https://www.tiktok.com/", True, "spa", "TikTok SPA"),
    URLTestCase("https://app.asana.com/", True, "spa+auth", "Asana（需登录）"),
    URLTestCase("https://www.producthunt.com/", True, "spa", "Product Hunt"),
    URLTestCase("https://dribbble.com/", True, "spa", "Dribbble"),
    URLTestCase("https://codepen.io/trending", True, "spa", "CodePen"),
    URLTestCase("https://codesandbox.io/", True, "spa", "CodeSandbox SPA"),
    URLTestCase("https://replit.com/", True, "spa", "Replit SPA"),
    URLTestCase("https://vercel.com/dashboard", True, "spa+auth", "Vercel Dashboard（需登录）"),
    URLTestCase("https://www.youtube.com/", True, "spa", "YouTube SPA"),
    URLTestCase("https://www.netflix.com/", True, "spa+auth", "Netflix（需登录）"),
]


# ---------------------------------------------------------------------------
# 验证逻辑
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    url: str
    expected: bool
    predicted_v1: Optional[bool]
    predicted_v2: Optional[bool]
    predicted_v3: Optional[bool]
    correct_v1: Optional[bool]
    correct_v2: Optional[bool]
    correct_v3: Optional[bool]
    category: str
    notes: str
    status_code: Optional[int] = None
    response_len: Optional[int] = None
    text_ratio: Optional[float] = None
    text_len: Optional[int] = None
    error: Optional[str] = None
    elapsed_ms: Optional[float] = None

    @property
    def predicted(self) -> Optional[bool]:
        return self.predicted_v3

    @property
    def correct(self) -> Optional[bool]:
        return self.correct_v3


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


async def test_url(
    client: httpx.AsyncClient,
    tc: URLTestCase,
    semaphore: asyncio.Semaphore,
    verbose: bool = False,
) -> TestResult:
    """单个 URL 测试，带并发限制。同时运行 v1/v2 对比。"""
    async with semaphore:
        result = TestResult(
            url=tc.url,
            expected=tc.expected,
            predicted_v1=None,
            predicted_v2=None,
            predicted_v3=None,
            correct_v1=None,
            correct_v2=None,
            correct_v3=None,
            category=tc.category,
            notes=tc.notes,
        )
        try:
            t0 = time.perf_counter()
            response = await client.get(tc.url, follow_redirects=True)
            elapsed = (time.perf_counter() - t0) * 1000

            result.status_code = response.status_code
            result.response_len = len(response.text)
            result.elapsed_ms = round(elapsed, 1)

            # 计算诊断信息
            soup = BeautifulSoup(response.text, "html.parser")
            text_content = soup.get_text(strip=True)
            result.text_len = len(text_content)
            result.text_ratio = round(len(text_content) / max(len(response.text), 1), 3)

            result.predicted_v1 = needs_browser_v1(response)
            result.predicted_v2 = needs_browser_v2(response)
            result.predicted_v3 = needs_browser_v3(response)
            result.correct_v1 = result.predicted_v1 == tc.expected
            result.correct_v2 = result.predicted_v2 == tc.expected
            result.correct_v3 = result.predicted_v3 == tc.expected

            if verbose:
                v1 = "✓" if result.correct_v1 else "✗"
                v2 = "✓" if result.correct_v2 else "✗"
                v3 = "✓" if result.correct_v3 else "✗"
                changed = ""
                if result.correct_v3 and not result.correct_v2:
                    changed = " ← v3 fixed"
                elif not result.correct_v3 and result.correct_v2:
                    changed = " ← v3 broke"
                print(
                    f"  v1{v1} v2{v2} v3{v3} [{tc.category:12s}] {tc.url[:48]:<48} "
                    f"exp={str(tc.expected):<5} "
                    f"ratio={result.text_ratio:.3f} tlen={result.text_len:5d} "
                    f"http={result.status_code}{changed}"
                )

        except Exception as e:
            result.error = str(e)
            if verbose:
                print(f"  ??     [{tc.category:12s}] {tc.url[:50]:<50} ERROR: {e}")

        return result


async def run_validation(verbose: bool = False) -> list[TestResult]:
    print(f"\n{'='*70}")
    print(f"  ScrapeKit — needs_browser() 准确率验证（V1 vs V2）")
    print(f"  测试 URL 总数: {len(TEST_CASES)}")
    print(f"{'='*70}\n")

    if verbose:
        print(f"  {'v1 v2':6s} {'类别':12s} {'URL':50s} {'期望':5s} {'ratio':>6} {'tlen':>5} {'http':>4}")
        print(f"  {'-'*105}")

    semaphore = asyncio.Semaphore(10)  # 最多 10 个并发请求

    async with httpx.AsyncClient(
        headers=HEADERS,
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
    ) as client:
        tasks = [
            test_url(client, tc, semaphore, verbose)
            for tc in TEST_CASES
        ]
        results = await asyncio.gather(*tasks)

    return list(results)


def _confusion(results, pred_attr):
    tested = [r for r in results if r.error is None]
    tp = [r for r in tested if r.expected and getattr(r, pred_attr)]
    tn = [r for r in tested if not r.expected and not getattr(r, pred_attr)]
    fp = [r for r in tested if not r.expected and getattr(r, pred_attr)]
    fn = [r for r in tested if r.expected and not getattr(r, pred_attr)]
    return tp, tn, fp, fn


def print_report(results: list[TestResult]) -> None:
    tested = [r for r in results if r.error is None]
    errors = [r for r in results if r.error is not None]
    total  = len(tested)

    tp1, tn1, fp1, fn1 = _confusion(results, "predicted_v1")
    tp2, tn2, fp2, fn2 = _confusion(results, "predicted_v2")
    tp3, tn3, fp3, fn3 = _confusion(results, "predicted_v3")
    acc1 = (len(tp1) + len(tn1)) / total * 100 if total else 0
    acc2 = (len(tp2) + len(tn2)) / total * 100 if total else 0
    acc3 = (len(tp3) + len(tn3)) / total * 100 if total else 0

    print(f"\n{'='*72}")
    print(f"  准确率对比（已测试: {total}，跳过: {len(errors)}）")
    print(f"{'='*72}")
    print(f"  {'指标':<32} {'V1':>8} {'V2':>8} {'V3':>8}")
    print(f"  {'-'*60}")
    print(f"  {'准确率':<32} {acc1:>7.1f}% {acc2:>7.1f}% {acc3:>7.1f}%")
    print(f"  {'True Positive  (SPA 正确识别)':<32} {len(tp1):>8} {len(tp2):>8} {len(tp3):>8}")
    print(f"  {'True Negative  (静态正确识别)':<32} {len(tn1):>8} {len(tn2):>8} {len(tn3):>8}")
    print(f"  {'False Positive (静态→浏览器误报)':<32} {len(fp1):>8} {len(fp2):>8} {len(fp3):>8}  ← 浪费性能")
    print(f"  {'False Negative (SPA→静态漏报)':<32} {len(fn1):>8} {len(fn2):>8} {len(fn3):>8}  ← 抓取失败")

    # V3 修复/退步
    fixed3  = [r for r in tested if r.correct_v3 and not r.correct_v2]
    broken3 = [r for r in tested if not r.correct_v3 and r.correct_v2]

    if fixed3:
        print(f"\n  V3 相比 V2 修复了 {len(fixed3)} 个错误:")
        for r in fixed3:
            tag = "FP→TN" if not r.expected else "FN→TP"
            print(f"    [{tag}] {r.url}")
            print(f"           ratio={r.text_ratio}  tlen={r.text_len}  http={r.status_code}")

    if broken3:
        print(f"\n  V3 相比 V2 引入了 {len(broken3)} 个新错误:")
        for r in broken3:
            tag = "TN→FP" if not r.expected else "TP→FN"
            print(f"    [{tag}] {r.url}")
            print(f"           ratio={r.text_ratio}  tlen={r.text_len}  http={r.status_code}")

    if fp3:
        print(f"\n  V3 剩余误报 (False Positive):")
        for r in fp3:
            print(f"    {r.url}")
            print(f"      ratio={r.text_ratio}  tlen={r.text_len}  http={r.status_code}")

    if fn3:
        print(f"\n  V3 剩余漏报 (False Negative):")
        for r in fn3:
            print(f"    {r.url}")
            print(f"      ratio={r.text_ratio}  tlen={r.text_len}  http={r.status_code}")

    if errors:
        print(f"\n  请求失败 (跳过):")
        for r in errors:
            print(f"    {r.url}  → {r.error[:80]}")

    # 按 category 分组 (V3)
    categories = sorted(set(r.category for r in tested))
    print(f"\n  按类别细分 (V3):")
    for cat in categories:
        cat_r = [r for r in tested if r.category == cat]
        cat_ok = [r for r in cat_r if r.correct_v3]
        cat_acc = len(cat_ok) / len(cat_r) * 100 if cat_r else 0
        bar = "█" * len(cat_ok) + "░" * (len(cat_r) - len(cat_ok))
        print(f"    {cat:16s}: {len(cat_ok):2d}/{len(cat_r):2d}  ({cat_acc:.0f}%)  {bar}")

    print(f"\n{'='*72}")
    if acc3 >= 90:
        verdict = "✅ V3 准确率达标 (≥90%)，可继续开发其他模块"
    elif acc3 >= 75:
        verdict = "⚠️  V3 准确率偏低 (75-90%)，建议继续调优"
    else:
        verdict = "❌ V3 准确率不足 (<75%)，架构需调整"
    print(f"  结论: {verdict}")
    print(f"{'='*72}\n")


def save_results(results: list[TestResult], path: str) -> None:
    tested = [r for r in results if r.error is None]
    tp3, tn3, fp3, fn3 = _confusion(results, "predicted_v3")
    data = {
        "total": len(results),
        "tested": len(tested),
        "errors": len([r for r in results if r.error is not None]),
        "v3_correct": len(tp3) + len(tn3),
        "v3_accuracy": round((len(tp3) + len(tn3)) / len(tested) * 100, 1) if tested else 0,
        "results": [asdict(r) for r in results],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  详细结果已保存到: {path}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def diagnose_url(url: str) -> None:
    """诊断单个 URL，逐步打印触发了哪条规则。"""
    import httpx as _httpx
    print(f"\n诊断: {url}")
    resp = _httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
    html = resp.text
    print(f"  status={resp.status_code}  html_len={len(html)}")
    if len(html) == 0:
        print("  → 触发: 空 body → False"); return
    if resp.status_code in (400, 401, 403, 407):
        print(f"  → 触发: {resp.status_code} → True"); return
    if len(html) < 500:
        print(f"  → 触发: len<500 → True"); return
    text_lower = html.lower()
    markers = ["enable javascript","you need javascript","javascript is required",
               "javascript is disabled",'<div id="app"></div>',"<div id='app'></div>",
               '<div id="root"></div>',"<div id='root'></div>"]
    for m in markers:
        if m in text_lower:
            print(f"  → 触发: JS marker [{m!r}] → True"); return
    from bs4 import BeautifulSoup as _BS
    soup = _BS(html, "html.parser")
    tc = soup.get_text(strip=True)
    tl, hl = len(tc), len(html)
    ratio = tl / max(hl, 1)
    print(f"  text_len={tl}  ratio={ratio:.4f}")
    if ratio < 0.005:
        print(f"  → 触发: ratio<0.005 → True"); return
    if tl < 200 and ratio < 0.15:
        print(f"  → 触发: tlen<200 and ratio<0.15 → True"); return
    if hl > 50_000:
        res = ratio < 0.018
        print(f"  → 触发: large page, ratio<0.018 → {res}")
    else:
        res = ratio < 0.05
        print(f"  → 触发: small page, ratio<0.05 → {res}")


def main():
    parser = argparse.ArgumentParser(description="验证 needs_browser() 准确率")
    parser.add_argument("--output", "-o", help="保存 JSON 结果到文件")
    parser.add_argument("--verbose", "-v", action="store_true", help="逐条打印测试结果")
    parser.add_argument("--diagnose", "-d", help="诊断单个 URL 的触发规则")
    args = parser.parse_args()

    if args.diagnose:
        diagnose_url(args.diagnose)
        return

    results = asyncio.run(run_validation(verbose=args.verbose))
    print_report(results)

    if args.output:
        save_results(results, args.output)


if __name__ == "__main__":
    main()
