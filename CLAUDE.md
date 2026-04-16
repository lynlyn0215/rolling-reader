# rolling-reader — Project Context

## 什么是 rolling-reader

本地优先的网页内容提取 CLI 工具，已发布至 PyPI，全局安装后命令为 `rr`。三层策略自动选择最优抓取方式，支持任意 URL（网页、REST API、SPA）。

当前版本：**v0.6.3**

```bash
rr <url>                              # 抓取单个 URL，输出 JSON
rr <url> --clean                      # 只要正文（trafilatura 过滤导航/广告/页脚）
rr <url> --output md                  # 输出 Markdown
rr <url> --json-path title            # 只取某个字段
rr batch urls.txt                     # 批量抓取（文件，一行一个 URL）
rr batch url1 url2 --concurrency 5    # 批量抓取（直接传 URL）
rr batch urls.txt --clean > out.jsonl # 批量正文提取，保存到文件
rr chrome                             # 启动 rr 专用 Chrome（调试模式）
```

## 三层策略

```
Level 1 — HTTP 直取（httpx + trafilatura）       ~500ms
Level 2 — CDP + rr 专用 Chrome Session          ~3s
Level 3 — JS State 提取（window.__PRELOADED_STATE__ 等）  ~1s
```

- Level 1 支持普通 SSR 网页和 **REST API endpoint**（如 `api.github.com`）
- Level 2/3 需要 Chrome 以调试模式运行（`rr chrome` 启动）
- SPA（如 36kr、Upwork、LinkedIn）Level 1 只能拿到框架 config，需 Level 2 才有内容
- 强制指定层级：`rr <url> --force-level 1`

## 目录结构

```
rolling-reader/
├── src/rolling_reader/
│   ├── cli.py          # 主入口：rr 命令 + launch_chrome()
│   ├── fetcher.py      # Level 1/2/3 策略
│   ├── cleaner.py      # trafilatura 正文提取
│   └── ...
├── pyproject.toml      # 版本号在这里
└── CLAUDE.md           # 本文件
```

## Chrome 登录态机制（重要）

rr 使用固定 Chrome profile：`~/.rolling-reader/chrome-profile/`

**Cookie 从真实 Chrome 同步的功能实际上不可靠（文件锁问题），忽略这个功能。**

正确的登录流程：
1. `rr chrome` → 启动 rr 专用 Chrome
2. 在这个 Chrome 里手动登录目标网站（X、LinkedIn 等）一次
3. 关闭 rr Chrome
4. 之后每次 `rr chrome` 都会加载同一个 profile，登录态自动保持

## 常见陷阱

| 问题 | 原因 | 解决 |
|------|------|------|
| "Chrome is not running" | Level 2/3 需要 rr Chrome 运行 | 先 `rr chrome`，保持后台运行 |
| SPA 页面内容为空 | Level 1 只拿到框架 config | 加 `--force-level 2`（需 Chrome） |
| 正文过长超 LLM 上下文 | 某些页面文字极多 | enrichment 用 MAX_BODY=8000 截断 |
| Cookie 同步后登录失效 | 同步功能不可靠 | 直接在 rr Chrome 里手动登录 |
| API endpoint 抓不到 | 误加 --force-level 2 | Level 1 即可，去掉 force-level |

## REST API 使用模式

rr Level 1 可以直接请求 JSON API，无需 Chrome：

```bash
rr https://api.github.com/repos/anthropics/claude-code   # GitHub API
rr https://api.github.com/repos/owner/repo/releases/latest
```

输出为 JSON，可配合 `--json-path` 取字段。

## 与 opencli-rs 的分工

| 场景 | 用哪个 |
|------|--------|
| HN 热榜、Reddit 帖子列表、Twitter 搜索 | opencli-rs（结构化） |
| 任意网页正文提取 | rr --clean |
| REST API endpoint | rr Level 1 |
| 需要登录的页面 | rr chrome → 手动登录 → rr |
| SPA 动态渲染 | rr --force-level 2（需 Chrome） |
| ~~Tavily / WebFetch~~ | ❌ 已禁用 |

## 发布到 PyPI

```bash
cd C:\Users\lamso\Desktop\rolling-reader
python -m build
TWINE_USERNAME=__token__ TWINE_PASSWORD=<token> twine upload dist/*
```

版本号在 `pyproject.toml` 的 `version` 字段，每次发布前更新。

## newsfeed 集成（enrichment pipeline）

`C:\Users\lamso\Desktop\newsfeed\collectors\__init__.py` 用 rr 做两件事：
1. `run_rr(url)` — 抓任意 URL 正文作为 RawArticle
2. `enrich_article()` — 对 body < 300 字的文章补全全文，上限 8000 字

ENRICH_SOURCES：hackernews、reddit-llama、reddit-ml、reddit-investing、twitter-following
