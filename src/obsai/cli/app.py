"""Typer entry point and the CLI error boundary."""

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from obsai import __version__
from obsai.config.loader import load_settings
from obsai.errors import ConfigError, ObsAIError
from obsai.logging import configure_logging

app = typer.Typer(help="ObsAgent command-line interface.", no_args_is_help=True)
index_app = typer.Typer(help="Maintain the local derived index.", no_args_is_help=True)
app.add_typer(index_app, name="index")
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


@index_app.command("update")
def index_update() -> None:
    """Synchronize changed Vault notes into the SQLite metadata index."""
    from obsai.indexing import IncrementalIndexer
    from obsai.storage import Database, IndexRepository

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    vault = settings.vault.path.expanduser()
    database_path = (settings.index.database or Path.home() / ".obsai" / "index.db").expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with Database(database_path) as database:
        result = IncrementalIndexer(IndexRepository(database)).update(vault)

    for label, kind in (
        ("Created", "created"),
        ("Modified", "modified"),
        ("Renamed", "renamed"),
        ("Moved", "moved"),
        ("Deleted", "deleted"),
        ("Unchanged", "unchanged"),
    ):
        console.print(f"{label}: {result.count(kind)}")
    console.print(f"Affected WikiLinks: {result.affected_link_count}")
    for change in result.changes:
        if change.affected_links:
            console.print(f"{change.old_path} → {change.path}")
            for link in change.affected_links:
                console.print(f"  {link.source_path}: [[{link.target_path or ''}]]")


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
