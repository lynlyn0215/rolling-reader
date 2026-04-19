# rolling-reader — Project Context

## 什么是 rolling-reader

本地优先的网页内容提取 CLI 工具，已发布至 PyPI，全局安装后命令为 `rr`。三层策略自动选择最优抓取方式，支持任意 URL（网页、REST API、SPA）。

当前版本：**v0.7.1**（本地 editable 安装，代码即最新）

```bash
rr <url>                              # 抓取单个 URL，输出 JSON
rr <url> --clean                      # 只要正文（trafilatura 过滤导航/广告/页脚）
rr <url> --text                       # 只输出纯文字到 stdout（管道友好）
rr <url> --select "article"           # 只抽 CSS 选择器匹配的节点
rr <url> --meta                       # 提取结构化元数据（og:*、JSON-LD、发布时间）
rr <url> --images                     # 附带图片 URL（og:image + 正文图）
rr <url> --rss                        # 解析 RSS/Atom feed 为结构化 JSON
rr <url> --output md                  # 输出 Markdown
rr <url> --json-path title            # 只取某个字段
rr <url> --retries 3                  # 429/503 时最多重试 N 次（指数退避）
rr batch urls.txt                     # 批量抓取（文件，一行一个 URL）
rr batch url1 url2 --concurrency 5    # 批量抓取（直接传 URL）
rr batch urls.txt --clean > out.jsonl # 批量正文提取，保存到文件
cat urls.txt | rr batch --clean       # stdin 管道模式
rr chrome                             # 启动 rr 专用 Chrome（调试模式）
```

## 三层策略

```
Level 1 — HTTP 直取（httpx + trafilatura）              ~500ms
Level 2 — CDP + rr 专用 Chrome Session                 ~3s
Level 3 — JS State 提取（window.__PRELOADED_STATE__ 等） ~1s
```

- Level 1 支持普通 SSR 网页和 **REST API endpoint**（如 `api.github.com`）
- Level 2/3 需要 Chrome 以调试模式运行（`rr chrome` 启动）
- SPA（如 36kr、Upwork、LinkedIn）Level 1 只能拿到框架 config，需 Level 2 才有内容
- 强制指定层级：`rr <url> --force-level 1`

## Profile Cache（v0.2）

- 按 domain 缓存最优层级，后续请求直接跳到已知层级
- TTL：从 `last_success` 起算 7 天（不是首次发现时间）
- 软失败：连续失败 3 次才真正清除，避免网络抖动误删
- L2/3 自动重探：每成功 20 次，悄悄试一次 L1，成功则自动降级
- 缓存文件位置：`~/.rolling-reader/profiles/<domain>.json`

## 目录结构

```
rolling-reader/
├── src/rolling_reader/
│   ├── cli.py                # 主入口：rr 命令 + launch_chrome()
│   ├── dispatcher.py         # 核心调度：L1→L2→L3 自动升级
│   ├── models.py             # ExtractResult、异常类
│   ├── extractor/
│   │   ├── http.py           # Level 1（httpx + BS4）
│   │   ├── cdp.py            # Level 2（CDP）
│   │   ├── state.py          # Level 3（JS state 提取）
│   │   └── clean.py          # trafilatura 正文提取
│   └── cache/
│       └── profile.py        # Profile Cache
├── CHANGELOG.md              # 版本历史
├── pyproject.toml            # 版本号在这里
└── CLAUDE.md                 # 本文件
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
| 正文过长超 LLM 上下文 | 某些页面文字极多 | 用 `--select` 精准抽取，或 enrichment 截断 8000 字 |
| API endpoint 抓不到 | 误加 --force-level 2 | Level 1 即可，去掉 force-level |
| Wikipedia 403 | Wikimedia 拒绝浏览器 UA | 已自动处理（bot UA 切换） |

## REST API 使用模式

rr Level 1 可以直接请求 JSON API，无需 Chrome：

```bash
rr https://api.github.com/repos/anthropics/claude-code   # GitHub API
rr https://hn.algolia.com/api/v1/items/39754398          # HN Algolia API
```

输出为 JSON，可配合 `--json-path` 取字段。

## 与 opencli-rs 的分工

| 场景 | 用哪个 |
|------|--------|
| HN 热榜、Reddit 帖子列表、Twitter 搜索 | opencli-rs（结构化） |
| 任意网页正文提取 | rr --clean |
| 只需特定区域 | rr --select "CSS选择器" |
| 需要元数据（发布时间、作者） | rr --meta |
| REST API endpoint | rr Level 1 |
| 需要登录的页面 | rr chrome → 手动登录 → rr |
| SPA 动态渲染 | rr --force-level 2（需 Chrome） |
| ~~Tavily / WebFetch~~ | ❌ 已禁用 |

## 安装状态

当前为 editable 模式安装（`pip install -e .`），改代码后直接生效，无需重装。
PyPI 最新发布版本为 v0.6.8（token 问题暂时无法发布新版）。

## 发布到 PyPI

```bash
cd C:\Users\<your-username>\Desktop\rolling-reader
python -m build
TWINE_USERNAME=__token__ TWINE_PASSWORD=<token> twine upload dist/*
```

版本号在 `pyproject.toml` 的 `version` 字段，每次发布前更新。

## newsfeed 集成（enrichment pipeline）

`C:\Users\<your-username>\Desktop\newsfeed\collectors\__init__.py` 用 rr 做两件事：
1. `run_rr(url)` — 抓任意 URL 正文作为 RawArticle
2. `enrich_article()` — 对 body < 300 字的文章补全全文，上限 8000 字

ENRICH_SOURCES：hackernews、reddit-llama、reddit-ml、reddit-investing、twitter-following
