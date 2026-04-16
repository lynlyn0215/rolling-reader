"""
rolling_reader/extractor/state.py
=============================
Level 3 — JS State 提取

核心思路：
  现代 SSR 框架（Next.js / Nuxt / 自定义）会在 HTML 里注入完整数据集，
  以 JavaScript 变量的形式存在（如 window.__PRELOADED_STATE__）。
  这些数据在 DOM 渲染之前就已存在，可以直接用 page.evaluate() 提取，
  比 DOM 解析更快、更完整、更稳定。

v0.1 范围：
  只支持 window.__PRELOADED_STATE__（已在 Sportsbet.com.au 生产验证）。
  其他变量（__NEXT_DATA__ / __NUXT__ 等）是 Phase 2 的扩展点。

与 Level 2 的关系：
  Level 3 复用 Level 2 的 CDP 页面加载流程，
  在页面加载完成后额外调用 page.evaluate() 尝试提取 state 变量。
  - 成功 → 返回 level=3 结果（结构化 JSON）
  - 失败 → 调用方回退到 Level 2 DOM 提取
"""

from __future__ import annotations

import json
from typing import Optional, Any

# v0.1 支持的 JS state 变量列表（按优先级排序）
# Phase 2 会扩展这个列表并加入 HAR 自动发现
KNOWN_STATE_VARS: list[str] = [
    "window.__PRELOADED_STATE__",
]


async def try_extract_state(
    page,  # playwright Page 对象
    state_vars: list[str] = KNOWN_STATE_VARS,
) -> tuple[Optional[str], Optional[Any]]:
    """
    尝试从已加载的页面中提取 JS state 变量。

    Args:
        page:       已导航到目标 URL 的 Playwright Page 对象
        state_vars: 要尝试的变量名列表（按优先级）

    Returns:
        (var_name, data) 如果找到
        (None, None)     如果均未找到
    """
    for var in state_vars:
        try:
            # page.evaluate 会把 JS 返回值序列化为 Python 对象
            # 如果变量不存在，JS 返回 undefined → Python 返回 None
            data = await page.evaluate(f"() => {var}")
            if data is not None:
                return var, data
        except Exception:
            # 变量不存在或 JS 执行出错，继续尝试下一个
            continue

    return None, None


def state_to_text(var_name: str, data: Any) -> str:
    """将 JS state 对象序列化为可读文本（JSON 格式）。"""
    return json.dumps(data, ensure_ascii=False, indent=2)
