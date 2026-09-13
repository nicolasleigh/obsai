"""Typer entry point and the CLI error boundary."""

import logging
from typing import Annotated

import typer
from rich.console import Console

from obsai import __version__
from obsai.config.loader import load_settings
from obsai.errors import ObsAIError
from obsai.logging import configure_logging

app = typer.Typer(help="ObsAgent command-line interface.", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)
logger = logging.getLogger(__name__)


def version_callback(value: bool) -> None:
    if value:
        console.print(f"obsai {__version__}")
        raise typer.Exit()


@app.callback()
def cli(
    version: Annotated[
        bool,
        typer.Option("--version", callback=version_callback, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Global CLI options."""


@app.command()
def status() -> None:
    """Show initial configuration status."""
    settings = load_settings()
    console.print("ObsAgent initialized")
    if settings.vault.path is None:
        console.print("No vault configured")
    else:
        console.print(f"Vault: {settings.vault.path}")


def main() -> None:
    """Console-script entry point with a shared boundary for domain errors."""
    configure_logging()
    try:
        app()
    except ObsAIError as exc:
        logger.debug("CLI error: %s", exc)
        error_console.print(f"Error: {exc}")
        raise SystemExit(2) from exc
    except Exception:
        logger.exception("Unexpected CLI error")
        error_console.print("Error: Unexpected failure")
        raise SystemExit(1) from None
