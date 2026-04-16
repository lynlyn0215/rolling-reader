"""
rolling_reader/models.py
===================
共享数据类型：ExtractResult、异常类。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class NeedsBrowserError(Exception):
    """Level 1 检测到页面需要浏览器渲染，应升级到 Level 2。"""
    def __init__(self, url: str, reason: str = ""):
        self.url = url
        self.reason = reason
        super().__init__(f"Browser required for {url}: {reason}")


class ExtractionError(Exception):
    """抓取过程中发生不可恢复的错误。"""
    def __init__(self, url: str, reason: str = ""):
        self.url = url
        self.reason = reason
        super().__init__(f"Extraction failed for {url}: {reason}")


# ---------------------------------------------------------------------------
# 抓取结果
# ---------------------------------------------------------------------------

@dataclass
class ExtractResult:
    """
    单次抓取的结构化结果。

    level:
        1 = HTTP 直取（httpx）
        2 = CDP + 已有 Chrome Session
        3 = JS State 提取
    """
    url: str
    level: int
    status_code: int
    title: str
    text: str           # 主要文字内容（BeautifulSoup 提取）
    links: list[str]    # 页面内所有链接（绝对 URL）
    elapsed_ms: float
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: Optional[str] = None

    # ── 输出格式 ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "level": self.level,
            "status_code": self.status_code,
            "title": self.title,
            "text": self.text,
            "links": self.links,
            "elapsed_ms": self.elapsed_ms,
            "extracted_at": self.extracted_at,
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_markdown(self) -> str:
        lines = [
            f"# {self.title or self.url}",
            f"",
            f"> URL: {self.url}  ",
            f"> Level: {self.level}  ",
            f"> Extracted: {self.extracted_at}",
            f"",
            "---",
            f"",
            self.text,
        ]
        if self.links:
            lines += ["", "---", "", "## Links", ""]
            lines += [f"- {link}" for link in self.links[:50]]
            if len(self.links) > 50:
                lines.append(f"- *(+{len(self.links) - 50} more)*")
        return "\n".join(lines)
