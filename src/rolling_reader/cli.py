"""
rolling_reader/cli.py
=================
CLI 入口（typer）

用法：
    scrape <url>
    scrape <url> --output md
    scrape <url> --force-level 2
    scrape <url> --verbose
"""

from __future__ import annotations

import asyncio
import sys
from enum import Enum
from typing import Optional

import typer

from rolling_reader.dispatcher import dispatch
from rolling_reader.models import ExtractionError

app = typer.Typer(
    name="scrape",
    help="Local-first web scraper with automatic strategy selection.",
    add_completion=False,
)


class OutputFormat(str, Enum):
    json = "json"
    md   = "md"


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
        help="Force a specific extraction level (1=HTTP, 2=CDP)",
        min=1,
        max=2,
    ),
    cdp_endpoint: str = typer.Option(
        "http://localhost:9222",
        "--cdp",
        help="Chrome DevTools endpoint",
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
            )
        )
    except ExtractionError as e:
        typer.echo(f"Error: {e.reason}", err=True)
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    if output == OutputFormat.json:
        typer.echo(result.to_json())
    else:
        typer.echo(result.to_markdown())
