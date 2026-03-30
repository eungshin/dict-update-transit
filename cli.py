"""
cli.py — Click-based CLI entry point for the dictionary tool.

Usage:
    python cli.py <word>
    dict <word>   (after pip install -e .)
"""

from __future__ import annotations

import io
import sys

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from dictionary import format_definition, lookup_word

# Reconfigure stdout/stderr to UTF-8 so IPA phonetic characters (and other
# Unicode glyphs) can be written on Windows where the default codepage is
# cp949/cp1252.  This must happen before creating the rich Console.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

console = Console()


@click.command()
@click.argument("word")
def main(word: str) -> None:
    """Look up WORD in the Free Dictionary and display its definition."""
    try:
        result = lookup_word(word)
    except Exception as exc:  # requests.RequestException or unexpected error
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(2)

    if result is None:
        console.print(
            f"[bold yellow]Word not found:[/bold yellow] '[italic]{word}[/italic]' "
            "was not found in the Free Dictionary."
        )
        sys.exit(1)

    formatted = format_definition(result)
    # Wrap in rich.text.Text to prevent IPA phonetics (contain [ ] brackets)
    # from being interpreted as rich markup tags.
    panel_content = Text(formatted)
    console.print(Panel(panel_content, title=f"[bold cyan]{result['word']}[/bold cyan]", expand=False))


if __name__ == "__main__":
    main()
