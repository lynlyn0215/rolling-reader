# Changelog

## v0.7.1 (2026-04-19)
- **新增** `--select` CSS 选择器模式：只提取匹配节点的文字（支持任意 BeautifulSoup CSS 语法）
- 匹配不到元素时 stderr 给出警告，返回空文字
- `--select` 优先级高于 `--clean`

## v0.7.0 (2026-04-19)
- **Smart Profile Cache v0.2**
  - TTL 改从 `last_success` 起算 7 天（原：`discovered_at` 起算 30 天）
  - 软失败：连续失败 3 次才真正清除 cache，避免网络抖动误删
  - L2/3 自动重探：每成功 20 次，下次命中时悄悄试一次 L1，成功则自动降级

## v0.6.9 (2026-04-18)
- **新增** `--meta` 结构化元数据提取
  - Open Graph（og:title / description / type / image / url / site_name）
  - Article（article:published_time / modified_time / author / section / tags）
  - JSON-LD（Article / NewsArticle / BlogPosting / WebPage 类型）
  - canonical URL、itemprop datePublished、标准 `<meta name=...>` 兜底
- `ExtractResult` 新增 `meta: dict` 字段

## v0.6.8 (2026-04-17)
- **新增** `--retries N`：429 / 503 时指数退避重试（默认 2 次，支持 Retry-After 头）
- **新增** `--text`：只输出正文文字到 stdout，管道友好（`rr <url> --text | llm`）
- **新增** stdin 管道模式：`cat urls.txt | rr batch --clean`
- 升级日志始终打印到 stderr（不再需要 `--verbose`）
- `NeedsBrowserError` 在 `--force-level 1` 时给出友好提示而非 traceback

## v0.6.7 (2026-04-16)
- **修复** Wikipedia / Wikimedia 403：为 Wikimedia 域名自动切换 bot UA
- **修复** GitHub 页面 ratio 误判：`large_page_low_ratio` 加 `text_len < 3000` 保险
- **修复** `NeedsBrowserError` 在 CLI 层未捕获导致 traceback

## v0.6.6 (2026-04-15)
- **新增** `--rss`：RSS 2.0 / Atom feed 结构化解析，返回 JSON 数组
- **新增** `--images`：提取 og:image + 正文图片 URL
- **修复** `status_code` 字段现在返回真实 HTTP 状态码（原来硬编码 200）
- **改进** `needs_browser()` V3：增加 403 / WAF 检测

## v0.6.5 (2026-04-14)
- `needs_browser()` V4：
  - Content-Type application/json → 永远不升级（API 端点保护）
  - 嵌入 state 保险（含 `__NEXT_DATA__` 的页面不升级）
  - 空 `<main>` 容器检测
  - `<noscript>` 剥离避免误判

## v0.6.1 (2026-04-10)
- `rr chrome` 改用持久化 profile（`~/.rolling-reader/chrome-profile/`）
- 登录态跨会话保留，不再需要每次重新登录

## v0.6.0 (2026-04-09)
- **新增** `rr chrome` 命令：一键启动带调试端口的 Chrome
- 自动检测 Chrome 路径（Windows / macOS / Linux）
- Chrome 未启动时给出明确错误提示

## v0.5.0 (2026-04-07)
- **新增** `rr batch`：并发批量抓取
  - 支持 URL 参数、文件路径、stdin 三种输入
  - `--concurrency N`（默认 3）
  - `--output jsonl|json`
  - Level 2/3 自动降为串行（Chrome 并发不稳定）

## v0.4.0 (2026-04-05)
- **新增** `--clean`：用 trafilatura 提取文章正文，过滤导航 / 广告 / 页脚

## v0.3.0 (2026-04-03)
- **新增** `--json-path`：dot-notation 取嵌套字段（`--json-path props.pageProps`）
- **新增** `--output md`：Markdown 输出
- `rr <url>` 无需写 `rr scrape <url>`（自动插入子命令）

## v0.2.0 (2026-04-01)
- Level 3 JS State 提取扩展至 12 个 window 变量
- 自动扫描未知 `window.VAR = {...}` 格式

## v0.1.0 (2026-03-28)
- 初始发布
- Level 1 HTTP（httpx + BeautifulSoup）
- Level 2 CDP（连接已有 Chrome）
- Level 3 JS State（`__NEXT_DATA__` 等）
- Profile Cache v0.1（domain 级别，30 天 TTL）
- `--force-level`、`--verbose`、`--no-cache`
