"""
rolling_reader/cli.py
=================
CLI 入口（typer）

用法：
    rr <url>
    rr <url> --output md
    rr <url> --force-level 2
    rr <url> --json-path props.pageProps
    rr <url> --no-cache --verbose
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
    json = "json"
    md   = "md"


def _resolve_json_path(data: dict, path: str):
    """
    按 dot-notation 路径从字典中取值。
    例：path="props.pageProps.title" → data["props"]["pageProps"]["title"]
    路径不存在时返回 None。
    """
    current = data
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


@app.command()
def scrape(
    url: str = typer.Argument(..., help="Target URL to scrape"),
    output: OutputFormat = typer.Option(
        OutputFormat.json,
        "--output", "-o",
        help="Output format: json (default) or md (markdown)",
    ),
    force_level: Optional[int] = typer.Option(
        None,
        "--force-level", "-l",
        help="Force a specific extraction level (1=HTTP, 2=CDP, 3=JS state)",
        min=1,
        max=3,
    ),
    json_path: Optional[str] = typer.Option(
        None,
        "--json-path", "-p",
        help=(
            "Dot-notation path into the result JSON. "
            "Top-level keys: url, level, title, text, links, elapsed_ms. "
            "For Level 3 JS state results, dig into nested data, e.g. --json-path text"
        ),
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass profile cache and always re-explore the best strategy",
    ),
    cdp_endpoint: str = typer.Option(
        "http://localhost:9222",
        "--cdp",
        help="Chrome DevTools endpoint (default: http://localhost:9222)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="Print escalation steps to stderr",
    ),
) -> None:
    """Scrape a URL and output structured data."""

    try:
        result = asyncio.run(
            dispatch(
                url,
                force_level=force_level,
                cdp_endpoint=cdp_endpoint,
                verbose=verbose,
                use_cache=not no_cache,
            )
        )
    except ExtractionError as e:
        _print_error(e)
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    # ── --json-path：从结果中提取指定字段 ──────────────────────────────────
    if json_path:
        value = _resolve_json_path(result.to_dict(), json_path)
        if value is None:
            typer.echo(
                f"Error: path '{json_path}' not found in result. "
                f"Available top-level keys: {', '.join(result.to_dict().keys())}",
                err=True,
            )
            raise typer.Exit(code=1)
        # 字符串直接输出，其他类型序列化为 JSON
        if isinstance(value, str):
            typer.echo(value)
        else:
            typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
        return

    # ── 正常输出 ────────────────────────────────────────────────────────────
    if output == OutputFormat.json:
        typer.echo(result.to_json())
    else:
        typer.echo(result.to_markdown())


def _print_error(e: ExtractionError) -> None:
    """格式化错误输出，给出可行动的提示。"""
    reason = e.reason or str(e)

    # Chrome 未启动
    if "Chrome is not available" in reason or "Cannot connect to Chrome" in reason:
        typer.echo(
            "\nError: Chrome is not running with remote debugging enabled.\n\n"
            "Start Chrome first:\n"
            "  macOS:   open -a 'Google Chrome' --args --remote-debugging-port=9222\n"
            "  Windows: chrome --remote-debugging-port=9222\n"
            "  Linux:   google-chrome --remote-debugging-port=9222\n",
            err=True,
        )
        return

    # 超时
    if "timeout" in reason.lower():
        typer.echo(
            f"\nError: Request timed out — {reason}\n\n"
            "Try: rr <url> --force-level 2  (use Chrome for slow-loading pages)\n",
            err=True,
        )
        return

    # 通用
    typer.echo(f"\nError: {reason}\n", err=True)
