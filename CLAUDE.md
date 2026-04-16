# ScrapeKit — Project Context

## 什么是 ScrapeKit

一个本地优先（local-first）的开源 CLI 网页提取工具，自动在 HTTP、已有 Chrome 浏览器会话、以及 JS state 提取之间切换策略。

```bash
scrape https://example.com   # 自动选择最优策略，输出 JSON
```

## Proposal 文档

`proposal-2026-04-15.md`（同目录）— v0.2，已经过 GPT / Claude / Grok 三方交叉评审。

## 技术栈

- Python 3.11+
- `httpx` + `beautifulsoup4`（Level 1 HTTP）
- `playwright` Python（Level 2 CDP + Level 3 state extraction）
  - `chromium.connect_over_cdp("http://localhost:9222")` 连接已有 Chrome
- `typer`（CLI）
- JSON 文件存 Profile Cache（`~/.scrapekit/profiles/`）
- PyPI 发布

## 策略阶梯

```
Level 1 — HTTP 直取（httpx）
Level 2 — CDP + 已有 Chrome Session（playwright connect_over_cdp）
Level 3 — JS State 提取（window.__PRELOADED_STATE__，v0.1 只支持这一个）
```

## MVP 范围（Phase 1）

- `scrape <url> [--output json|md] [--force-level N]`
- 三层策略自动升级
- Level 3 只支持 `window.__PRELOADED_STATE__`（已在 Sportsbet.com.au 生产验证）
- Profile Cache：domain 级别，JSON 文件存本地
- JSON 输出（默认）

## 第一步（最重要）

**在写任何其他模块之前，先验证 `needs_browser()` 的准确率。**

在 50+ 个真实 URL 上测试这个函数，覆盖：静态页、SPA、需要登录的页面、Cloudflare 保护页。这是整个架构的核心瓶颈，准确率不达标则架构需要调整。

## 已验证的核心机制（来自 nba-parlay-system 项目）

- CDP 连接已有 Chrome session：在 Windows 生产环境跑通
- HAR 录制发现 `window.__PRELOADED_STATE__`：在 Sportsbet.com.au 验证
- `__PRELOADED_STATE__` 提取：12 次抓取，结构化 JSON 输出
- 速度：DOM 抓取 23 秒 → State 提取 6 秒/页，4 场并行 ~8 秒

## 差异化（对比 Scrapling 37k stars 等现有工具）

| 能力 | 现有工具 | ScrapeKit |
|------|---------|-----------|
| 自动策略选择 | ❌ | ✅ |
| 复用已登录 Chrome | ❌ | ✅ |
| JS State 提取 | ❌ | ✅ |
| 纯 Python，无 Node.js | ✅ | ✅ |
