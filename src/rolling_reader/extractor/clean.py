"""
rolling_reader/extractor/clean.py
==================================
正文提取（Article Extraction）

使用 trafilatura 从 HTML 中识别并提取主体文章内容，
过滤导航栏、广告、页脚、侧边栏等噪音。

对比默认的 BeautifulSoup 文本提取：
  默认：把 <body> 里所有文字全部返回（快，但夹杂噪音）
  --clean：只返回主体文章文字（慢约 50ms，但干净）
"""

from __future__ import annotations
from typing import Optional


def clean_extract(html: str, url: str = "") -> Optional[str]:
    """
    从 HTML 中提取正文。

    Args:
        html: 完整 HTML 字符串
        url:  原始 URL（trafilatura 用于辅助判断，可选）

    Returns:
        正文文字，或 None（trafilatura 无法识别正文时）
    """
    try:
        import trafilatura
    except ImportError:
        raise ImportError(
            "trafilatura is required for --clean mode: pip install trafilatura"
        )

    text = trafilatura.extract(
        html,
        url=url or None,
        include_comments=False,
        include_tables=True,
        no_fallback=False,   # 允许回退到其他算法
        favor_precision=True,
    )
    return text or None
