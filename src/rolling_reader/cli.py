"""
rolling_reader/cli.py
=================
CLI 入口（typer）

用法：
    rr <url>
    rr <url> --output md --clean
    rr <url> --force-level 2 --json-path props.pageProps
    rr batch urls.txt
    rr batch url1 url2 url3 --concurrency 5 --clean
"""

from __future__ import annotations

import asyncio
import json
import sys
from enum import Enum
from typing import Optional

import typer

from rolling_reader.dispatcher import dispatch
from rolling_reader.models import ExtractionError

app = typer.Typer(
    name="rr",
    help="Local-first web scraper — automatically selects HTTP, CDP, or JS state extraction.",
    add_completion=False,
)


class OutputFormat(str, Enum):
    json  = "json"
    md    = "md"


class BatchOutputFormat(str, Enum):
    jsonl = "jsonl"   # 每行一个 JSON（默认，适合管道处理）
    json  = "json"    # 单个 JSON 数组


def _resolve_json_path(data: dict, path: str):
    """按 dot-notation 路径从字典中取值，路径不存在返回 None。"""
    current = data
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


# ---------------------------------------------------------------------------
# 单 URL 命令
# ---------------------------------------------------------------------------

@app.command(name="scrape", hidden=True)  # rr scrape <url>（隐藏，推荐直接 rr <url>）
def scrape_cmd(
    url: str = typer.Argument(..., help="Target URL to scrape"),
    output: OutputFormat = typer.Option(OutputFormat.json, "--output", "-o"),
    force_level: Optional[int] = typer.Option(None, "--force-level", "-l", min=1, max=3),
    json_path: Optional[str] = typer.Option(None, "--json-path", "-p"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    cdp_endpoint: str = typer.Option("http://localhost:9222", "--cdp"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    clean: bool = typer.Option(False, "--clean", "-c"),
) -> None:
    """Scrape a single URL."""
    _run_scrape(url, output, force_level, json_path, no_cache, cdp_endpoint, verbose, clean)


def _run_scrape(
    url: str,
    output: OutputFormat,
    force_level: Optional[int],
    json_path: Optional[str],
    no_cache: bool,
    cdp_endpoint: str,
    verbose: bool,
    clean: bool,
) -> None:
    """单 URL 抓取的核心逻辑（被 callback 和 scrape 共用）。"""
    try:
        result = asyncio.run(dispatch(
            url,
            force_level=force_level,
            cdp_endpoint=cdp_endpoint,
            verbose=verbose,
            use_cache=not no_cache,
            clean=clean,
        ))
    except ExtractionError as e:
        _print_error(e)
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    if json_path:
        value = _resolve_json_path(result.to_dict(), json_path)
        if value is None:
            typer.echo(
                f"Error: path '{json_path}' not found. "
                f"Available keys: {', '.join(result.to_dict().keys())}",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
        return

    typer.echo(result.to_json() if output == OutputFormat.json else result.to_markdown())


# ---------------------------------------------------------------------------
# 批量命令
# ---------------------------------------------------------------------------

@app.command()
def batch(
    inputs: list[str] = typer.Argument(
        ...,
        help="URLs to scrape, or a path to a text file containing one URL per line",
    ),
    output: BatchOutputFormat = typer.Option(
        BatchOutputFormat.jsonl,
        "--output", "-o",
        help="Output format: jsonl (default, one JSON per line) or json (array)",
    ),
    concurrency: int = typer.Option(
        3, "--concurrency", "-n",
        help="Number of concurrent requests (default: 3; auto-reduced to 1 when Chrome is needed)",
        min=1, max=20,
    ),
    force_level: Optional[int] = typer.Option(
        None, "--force-level", "-l",
        help="Force a specific extraction level (1=HTTP, 2=CDP, 3=JS state)",
        min=1, max=3,
    ),
    clean: bool = typer.Option(
        False, "--clean", "-c",
        help="Extract article body only, filtering out navigation, ads, and footers",
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache",
        help="Bypass profile cache",
    ),
    cdp_endpoint: str = typer.Option(
        "http://localhost:9222", "--cdp",
        help="Chrome DevTools endpoint",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Print per-URL progress to stderr",
    ),
) -> None:
    """Scrape multiple URLs in parallel.

    Accepts URL arguments directly, or a path to a .txt file (one URL per line).

    Examples:

        rr batch urls.txt

        rr batch https://example.com https://hn.algolia.com

        rr batch urls.txt --clean --output jsonl > results.jsonl
    """
    urls = _resolve_inputs(inputs)
    if not urls:
        typer.echo("Error: no URLs found.", err=True)
        raise typer.Exit(code=1)

    # Level 2/3 需要 Chrome，多标签并发容易出错，自动降为串行
    effective_concurrency = concurrency
    if force_level in (2, 3):
        effective_concurrency = 1
        if verbose:
            typer.echo("Note: --force-level 2/3 uses Chrome; concurrency forced to 1.", err=True)

    typer.echo(f"Scraping {len(urls)} URLs (concurrency={effective_concurrency})...", err=True)

    results = asyncio.run(_run_batch(
        urls,
        concurrency=effective_concurrency,
        force_level=force_level,
        clean=clean,
        no_cache=no_cache,
        cdp_endpoint=cdp_endpoint,
        verbose=verbose,
    ))

    # ── 输出 ────────────────────────────────────────────────────────────────
    if output == BatchOutputFormat.jsonl:
        for r in results:
            typer.echo(json.dumps(r, ensure_ascii=False))
    else:
        typer.echo(json.dumps(results, ensure_ascii=False, indent=2))

    # ── 汇总（stderr，不污染 stdout 的数据流）───────────────────────────────
    ok    = sum(1 for r in results if not r.get("error"))
    fail  = sum(1 for r in results if r.get("error"))
    typer.echo(f"\nDone: {ok} ok, {fail} failed out of {len(urls)}", err=True)
    if fail:
        raise typer.Exit(code=1)


def _resolve_inputs(inputs: list[str]) -> list[str]:
    """
    把命令行 inputs 解析为 URL 列表。
    - 如果只有一个参数且看起来像文件路径 → 读文件
    - 否则直接当 URL 列表用
    """
    import os
    if len(inputs) == 1 and not inputs[0].startswith("http"):
        path = inputs[0]
        if not os.path.exists(path):
            typer.echo(f"Error: file not found: {path}", err=True)
            raise typer.Exit(code=1)
        with open(path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        return lines
    return [u for u in inputs if u.strip()]


async def _run_batch(
    urls: list[str],
    *,
    concurrency: int,
    force_level: Optional[int],
    clean: bool,
    no_cache: bool,
    cdp_endpoint: str,
    verbose: bool,
) -> list[dict]:
    """并发执行批量抓取，返回结果列表（保持输入顺序）。"""
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict | None] = [None] * len(urls)

    async def fetch(idx: int, url: str) -> None:
        async with semaphore:
            try:
                result = await dispatch(
                    url,
                    force_level=force_level,
                    cdp_endpoint=cdp_endpoint,
                    verbose=False,
                    use_cache=not no_cache,
                    clean=clean,
                )
                results[idx] = result.to_dict()
                if verbose:
                    typer.echo(f"  ✓ [{idx+1}/{len(urls)}] L{result.level} {url} ({result.elapsed_ms:.0f}ms)", err=True)
            except Exception as e:
                results[idx] = {"url": url, "error": str(e)}
                if verbose:
                    typer.echo(f"  ✗ [{idx+1}/{len(urls)}] {url}  {e}", err=True)

    await asyncio.gather(*[fetch(i, u) for i, u in enumerate(urls)])
    return results  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# rr chrome — 启动 Chrome（带调试端口）
# ---------------------------------------------------------------------------

@app.command(name="chrome")
def launch_chrome(
    port: int = typer.Option(9222, "--port", "-p", help="Remote debugging port (default: 9222)"),
) -> None:
    """Launch Chrome with remote debugging enabled.

    Finds Chrome automatically and starts it in the background.
    After running this, Level 2/3 scraping works immediately.

    Example:

        rr chrome
        rr https://app.example.com/dashboard
    """
    import subprocess
    import platform

    import asyncio
    import time
    import os
    import tempfile

    # 先检查端口是否已经在用（Chrome 已经以调试模式运行）
    if asyncio.run(_check_cdp(port)):
        typer.echo(f"Chrome is already running with remote debugging on port {port}.")
        typer.echo("Ready — run: rr <url>")
        return

    exe = _find_chrome()
    if exe is None:
        typer.echo(
            "Error: Chrome not found. Install Google Chrome and try again.\n"
            "Download: https://www.google.com/chrome/",
            err=True,
        )
        raise typer.Exit(code=1)

    # 用独立的 user-data-dir，避免被已有 Chrome 进程吞掉
    debug_profile = os.path.join(tempfile.gettempdir(), "rolling-reader-chrome")
    os.makedirs(debug_profile, exist_ok=True)

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={debug_profile}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(
                args,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            subprocess.Popen(args, start_new_session=True, close_fds=True)
    except Exception as e:
        typer.echo(f"Error: failed to launch Chrome: {e}", err=True)
        raise typer.Exit(code=1)

    # 等待 Chrome 初始化调试端口（最多 8 秒）
    typer.echo("Starting Chrome...", err=True)
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.5)
        if asyncio.run(_check_cdp(port)):
            typer.echo(f"Chrome ready on port {port}.")
            typer.echo("Now run: rr <url>")
            return

    typer.echo(
        f"Warning: Chrome launched but port {port} is not responding yet.\n"
        "Wait a moment and try your rr command — it may still be starting up.",
        err=True,
    )


async def _check_cdp(port: int) -> bool:
    """检查 CDP 端口是否已经在响应。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            r = await client.get(f"http://localhost:{port}/json/version")
            return r.status_code == 200
    except Exception:
        return False


def _find_chrome() -> Optional[str]:
    """在各平台上自动定位 Chrome 可执行文件。"""
    import platform
    import shutil

    system = platform.system()

    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(
                __import__("os").environ.get("USERNAME", "")
            ),
        ]
        for path in candidates:
            if __import__("os.path", fromlist=["exists"]).exists(path):
                return path
        # 尝试 registry
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
            )
            path, _ = winreg.QueryValueEx(key, "")
            if path:
                return path
        except Exception:
            pass

    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
        for path in candidates:
            if __import__("os.path", fromlist=["exists"]).exists(path):
                return path

    else:  # Linux
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                return found

    return None


# ---------------------------------------------------------------------------
# 真正的 CLI 入口
# ---------------------------------------------------------------------------

_SUBCOMMANDS = {"batch", "scrape", "chrome", "--help", "-h", "--version"}


def main() -> None:
    """
    入口包装：让 `rr <url>` 直接工作，无需写 `rr scrape <url>`。

    如果第一个参数不是已知子命令，自动在前面插入 'scrape'。
    """
    args = sys.argv[1:]
    if args and not args[0].startswith("-") and args[0] not in _SUBCOMMANDS:
        sys.argv.insert(1, "scrape")
    app()


# ---------------------------------------------------------------------------
# 错误提示
# ---------------------------------------------------------------------------

def _print_error(e: ExtractionError) -> None:
    reason = e.reason or str(e)
    if "Chrome is not available" in reason or "Cannot connect to Chrome" in reason:
        typer.echo(
            "\nError: Chrome is not running with remote debugging enabled.\n\n"
            "Fix: run this first, then retry:\n"
            "  rr chrome\n",
            err=True,
        )
        return
    if "timeout" in reason.lower():
        typer.echo(
            f"\nError: Request timed out — {reason}\n\n"
            "Try: rr <url> --force-level 2\n",
            err=True,
        )
        return
    typer.echo(f"\nError: {reason}\n", err=True)
