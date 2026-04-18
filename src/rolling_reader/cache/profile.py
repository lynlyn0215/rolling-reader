"""
rolling_reader/cache/profile.py
===========================
Profile Cache — 按 domain 缓存最优抓取策略。

作用：
  第一次访问某站点时，rolling-reader 会探索最优策略（Level 1 / 2 / 3）。
  成功后，将"配方"写入本地 JSON 文件。
  后续请求直接跳到已知的最优层级，跳过探索开销。

存储位置：~/.rolling-reader/profiles/<domain>.json

配方格式：
  {
    "domain": "sportsbet.com.au",
    "preferred_level": 3,
    "state_var": "window.__PRELOADED_STATE__",   // Level 3 专用
    "discovered_at": "2026-04-16T00:00:00Z",
    "last_success": "2026-04-16T00:08:00Z",
    "success_count": 12,
    "failure_count": 0,
    "reprobe_due": false    // L2/3 专用：下次命中时悄悄试一次 L1
  }

v0.2 改动：
  - TTL 改为从 last_success 起算 7 天（原：discovered_at 起算 30 天）
  - 软失败：连续失败 3 次才真正清除 cache（原：任何失败立即清除）
  - L2/3 自动重探：每成功 REPROBE_INTERVAL 次，设 reprobe_due=true
    dispatcher 收到此标志后悄悄试一次 L1，成功则降级
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# 缓存目录
CACHE_DIR = Path.home() / ".rolling-reader" / "profiles"

# 配方有效期：从 last_success 起算，N 天没成功就视为过期
STALE_DAYS = 7

# 连续失败多少次才真正清除 cache（软失败阈值）
SOFT_FAIL_THRESHOLD = 3

# L2/3 profile 每成功多少次触发一次 L1 重探
REPROBE_INTERVAL = 20


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    """从 URL 提取 domain（不含 www.）。"""
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    host = host.split(":")[0]
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

    过期判断：从 last_success 起算超过 STALE_DAYS 天即过期。
    （注：老格式只有 discovered_at 的 profile 会兼容处理）

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

    # TTL：优先用 last_success，兼容老格式用 discovered_at
    anchor = profile.get("last_success") or profile.get("discovered_at", "")
    if anchor:
        try:
            dt = datetime.fromisoformat(anchor)
            if datetime.now(timezone.utc) - dt > timedelta(days=STALE_DAYS):
                path.unlink(missing_ok=True)
                return None
        except ValueError:
            pass

    return profile


def save(url: str, result_level: int, state_var: Optional[str] = None) -> None:
    """
    保存或更新 domain 的缓存配方。

    成功时：重置 failure_count，更新 last_success，
    并在 success_count 达到 REPROBE_INTERVAL 倍数时设置 reprobe_due（仅 L2/3）。
    """
    domain = _domain(url)
    path = _profile_path(domain)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    now = _now_iso()
    new_success_count = existing.get("success_count", 0) + 1

    # L2/3：每 REPROBE_INTERVAL 次成功，标记下次悄悄试 L1
    reprobe_due = (
        result_level >= 2
        and new_success_count % REPROBE_INTERVAL == 0
    )

    profile = {
        "domain": domain,
        "preferred_level": result_level,
        "state_var": state_var,
        "discovered_at": existing.get("discovered_at", now),
        "last_success": now,
        "success_count": new_success_count,
        "failure_count": 0,          # 成功后重置
        "reprobe_due": reprobe_due,
    }

    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def record_failure(url: str) -> bool:
    """
    记录一次抓取失败。

    软失败：累计失败 < SOFT_FAIL_THRESHOLD 时只增计数，不删 cache。
    硬失败：达到阈值时删除 cache，触发下次重新探索。

    Returns:
        True  — 已硬失效（cache 被删除）
        False — 软失败，cache 保留，下次仍会命中
    """
    domain = _domain(url)
    path = _profile_path(domain)

    if not path.exists():
        return True  # 本来就没有，相当于已失效

    try:
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
    except (json.JSONDecodeError, OSError):
        path.unlink(missing_ok=True)
        return True

    failure_count = profile.get("failure_count", 0) + 1

    if failure_count >= SOFT_FAIL_THRESHOLD:
        path.unlink(missing_ok=True)
        return True  # 硬失效

    # 软失败：更新计数后写回
    profile["failure_count"] = failure_count
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError:
        pass
    return False


def invalidate(url: str) -> None:
    """立即删除 cache（兼容旧调用；新代码优先用 record_failure）。"""
    _profile_path(_domain(url)).unlink(missing_ok=True)


def list_profiles() -> list[dict]:
    """列出所有已缓存的配方。"""
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
