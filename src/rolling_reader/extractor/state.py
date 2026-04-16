"""
rolling_reader/extractor/state.py
=============================
Level 3 — JS State 提取

核心思路：
  现代 SSR 框架（Next.js / Nuxt / SvelteKit / 自定义）会在 HTML 里注入完整数据集，
  以 JavaScript 变量的形式存在（如 window.__NEXT_DATA__）。
  这些数据在 DOM 渲染之前就已存在，可以直接用 page.evaluate() 提取，
  比 DOM 解析更快、更完整、更稳定。

v0.2 改动：
  - 扩展 KNOWN_STATE_VARS（Next.js / Nuxt / Redux 等主流框架）
  - 新增 auto_scan_state()：扫 <script> 标签里的未知大 JSON 对象
  - 框架感知提取：__NEXT_DATA__ 自动钻入 props.pageProps

与 Level 2 的关系：
  Level 3 复用 Level 2 的 CDP 页面加载流程，
  在页面加载完成后额外调用 page.evaluate() 尝试提取 state 变量。
  - 成功 → 返回 level=3 结果（结构化 JSON）
  - 失败 → 调用方回退到 Level 2 DOM 提取
"""

from __future__ import annotations

import json
import re
from typing import Optional, Any

# ---------------------------------------------------------------------------
# 已知 JS state 变量（按常见程度排序）
# ---------------------------------------------------------------------------

KNOWN_STATE_VARS: list[str] = [
    "window.__NEXT_DATA__",         # Next.js（Vercel 生态，极其普遍）
    "window.__NUXT__",              # Nuxt.js
    "window.__PRELOADED_STATE__",   # Redux / 自定义（v0.1 已验证）
    "window.__INITIAL_STATE__",     # 各类框架
    "window.__REDUX_STATE__",       # Redux explicit naming
    "window.__APP_STATE__",         # 各类框架
    "window.__STATE__",             # 通用
    "window.__STORE__",             # MobX / 自定义
    "window.APP_STATE",             # 无下划线变体
    "window.initialState",          # camelCase 变体
    "window.__remixContext",        # Remix
    "window.__staticRouterHydrationData",  # React Router v6 SSR
]

# auto_scan 的最小有效 JSON 字节数（过滤掉小型配置对象）
_AUTO_SCAN_MIN_BYTES = 1_000

# auto_scan 用于匹配 window.VAR = {...} 的正则
# 只捕获变量名，数据通过 page.evaluate 取（避免正则解析 JSON 的陷阱）
_WINDOW_VAR_RE = re.compile(
    r'window\.([A-Za-z_$][A-Za-z0-9_$]*)(?:\s*[=:]\s*)(\{|\[)',
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# 框架感知：从 state 里提取最有价值的子集
# ---------------------------------------------------------------------------

_FRAMEWORK_EXTRACTORS: dict[str, list[str]] = {
    # Next.js：核心数据在 props.pageProps
    "window.__NEXT_DATA__": ["props", "pageProps"],
    # Nuxt.js：核心数据在 data 或 state
    "window.__NUXT__": ["data"],
    # Remix：核心数据在 state
    "window.__remixContext": ["state", "loaderData"],
}


def _deep_get(data: Any, path: list[str]) -> Any:
    """按路径深层取值，任意节点缺失时返回原始 data。"""
    current = data
    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return data   # 路径不存在，返回完整 data
    return current


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

async def try_extract_state(
    page,
    state_vars: list[str] = KNOWN_STATE_VARS,
    auto_scan: bool = True,
) -> tuple[Optional[str], Optional[Any]]:
    """
    尝试从已加载的页面中提取 JS state 变量。

    策略：
      1. 按优先级逐一尝试 KNOWN_STATE_VARS
      2. 若均未命中，调用 auto_scan_state() 扫描未知变量

    Args:
        page:       已导航到目标 URL 的 Playwright Page 对象
        state_vars: 要尝试的变量名列表（按优先级）
        auto_scan:  是否在已知变量均未命中时自动扫描

    Returns:
        (var_name, data) 如果找到
        (None, None)     如果均未找到
    """
    # 第一轮：已知变量
    for var in state_vars:
        try:
            data = await page.evaluate(f"() => {var}")
            if data is not None:
                # 框架感知：提取最有价值的子集
                if var in _FRAMEWORK_EXTRACTORS:
                    path = _FRAMEWORK_EXTRACTORS[var]
                    data = _deep_get(data, path)
                return var, data
        except Exception:
            continue

    # 第二轮：自动扫描
    if auto_scan:
        return await auto_scan_state(page)

    return None, None


async def auto_scan_state(page) -> tuple[Optional[str], Optional[Any]]:
    """
    扫描 <script> 标签，寻找未在 KNOWN_STATE_VARS 中列出的大型 JSON 对象。

    策略：
      1. 从 page.content() 拿到原始 HTML
      2. 用正则找所有 window.VAR = { 或 window.VAR = [ 形式的赋值
      3. 跳过已知变量和浏览器内置名
      4. 用 page.evaluate() 实际取值（让 JS 引擎做反序列化，比 regex 解 JSON 可靠）
      5. 返回第一个超过阈值的候选

    Returns:
        (var_name, data) 或 (None, None)
    """
    try:
        html = await page.content()
    except Exception:
        return None, None

    # 从 HTML 里提取候选变量名
    candidates: list[str] = []
    seen: set[str] = set()
    known_set = set(KNOWN_STATE_VARS)

    for match in _WINDOW_VAR_RE.finditer(html):
        var_name = f"window.{match.group(1)}"
        if var_name not in known_set and var_name not in seen:
            # 跳过常见的非数据全局变量
            raw = match.group(1)
            if raw in _BROWSER_BUILTINS:
                continue
            candidates.append(var_name)
            seen.add(var_name)

    # 逐个尝试
    for var in candidates:
        try:
            data = await page.evaluate(f"() => {var}")
            if data is None:
                continue
            # 只接受足够大的对象（过滤 GA/GTM 等小型配置）
            serialized = json.dumps(data, ensure_ascii=False)
            if len(serialized.encode()) >= _AUTO_SCAN_MIN_BYTES:
                return var, data
        except Exception:
            continue

    return None, None


# 浏览器内置全局变量名黑名单（不值得尝试提取）
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
    # 常见第三方（非数据）
    "ga", "gtag", "dataLayer", "fbq", "twq",
    "Intercom", "analytics", "mixpanel", "amplitude",
})


def state_to_text(var_name: str, data: Any) -> str:
    """将 JS state 对象序列化为可读文本（JSON 格式）。"""
    return json.dumps(data, ensure_ascii=False, indent=2)
