"""
scrapekit/cache/profile.py
===========================
Profile Cache — 按 domain 缓存最优抓取策略。

作用：
  第一次访问某站点时，ScrapeKit 会探索最优策略（Level 1 / 2 / 3）。
  成功后，将"配方"写入本地 JSON 文件。
  后续请求直接跳到已知的最优层级，跳过探索开销。

存储位置：~/.scrapekit/profiles/<domain>.json

配方格式：
  {
    "domain": "sportsbet.com.au",
    "preferred_level": 3,
    "state_var": "window.__PRELOADED_STATE__",   // Level 3 专用
    "discovered_at": "2026-04-16T00:00:00Z",
    "last_success": "2026-04-16T00:08:00Z",
    "success_count": 12,
    "failure_count": 0
  }

v0.1 范围：
  - domain 级别匹配（保守，安全）
  - 30 天过期
  - 提取失败时立即失效，触发重新探索
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# 缓存目录
CACHE_DIR = Path.home() / ".scrapekit" / "profiles"

# 配方有效期（天）
STALE_DAYS = 30


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    """从 URL 提取 domain（不含 www.）。"""
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    # 去掉端口
    host = host.split(":")[0]
    # 去掉 www.
    if host.startswith("www."):
        host = host[4:]
    return host.lower()


def _profile_path(domain: str) -> Path:
    return CACHE_DIR / f"{domain}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def load(url: str) -> Optional[dict]:
    """
    读取 domain 的缓存配方。

    Returns:
        配方 dict，或 None（未命中 / 已过期）
    """
    domain = _domain(url)
    path = _profile_path(domain)

    if not path.exists():
        return None

    try:
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # 检查是否过期
    discovered = profile.get("discovered_at", "")
    if discovered:
        try:
            dt = datetime.fromisoformat(discovered)
            if datetime.now(timezone.utc) - dt > timedelta(days=STALE_DAYS):
                path.unlink(missing_ok=True)
                return None
        except ValueError:
            pass

    return profile


def save(url: str, result_level: int, state_var: Optional[str] = None) -> None:
    """
    保存或更新 domain 的缓存配方。

    Args:
        url:          成功抓取的 URL
        result_level: 实际使用的层级（1 / 2 / 3）
        state_var:    Level 3 时用到的 JS 变量名
    """
    domain = _domain(url)
    path = _profile_path(domain)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 读取已有记录（更新计数器）
    existing: dict = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    now = _now_iso()
    profile = {
        "domain": domain,
        "preferred_level": result_level,
        "state_var": state_var,
        "discovered_at": existing.get("discovered_at", now),
        "last_success": now,
        "success_count": existing.get("success_count", 0) + 1,
        "failure_count": existing.get("failure_count", 0),
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def invalidate(url: str) -> None:
    """提取失败时调用，立即删除缓存配方，触发下次重新探索。"""
    domain = _domain(url)
    path = _profile_path(domain)
    if path.exists():
        # 增加失败计数，但不删除（保留历史，只标记失效）
        try:
            with open(path, encoding="utf-8") as f:
                profile = json.load(f)
            profile["failure_count"] = profile.get("failure_count", 0) + 1
            profile["discovered_at"] = ""   # 清空，下次 load() 会返回 None
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile, f, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)


def list_profiles() -> list[dict]:
    """列出所有已缓存的配方（用于 scrape profile list 命令，Phase 2）。"""
    if not CACHE_DIR.exists():
        return []
    profiles = []
    for p in sorted(CACHE_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                profiles.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return profiles
