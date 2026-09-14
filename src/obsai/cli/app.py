"""Typer entry point and the CLI error boundary."""

import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from pydantic import ValidationError

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


def _embedding_pipeline(database):
    from obsai.embedding.models import EmbeddingGeneration
    from obsai.embedding.openai_provider import OpenAIEmbeddingProvider
    from obsai.embedding.pipeline import EmbeddingPipeline
    from obsai.storage.vectors import SQLiteVectorStore

    settings = load_settings()
    config = settings.embedding
    generation = EmbeddingGeneration(
        config.provider, config.model, config.model_version or config.model,
        config.dimensions,
    )
    provider = OpenAIEmbeddingProvider(generation, config.timeout_seconds)
    store = SQLiteVectorStore(database)
    return store, EmbeddingPipeline(store, provider, config)


def _semantic_retriever(database, query: str, *, strict: bool = False):
    from obsai.retrieval import VectorRetriever

    semantic = None
    reason = "Semantic index missing; run 'obsai index embeddings'; using keyword results"
    try:
        store, pipeline = _embedding_pipeline(database)
        if store.has_generation(pipeline.generation) and query.strip():
            tokens = pipeline.count_tokens(query)
            pipeline._check_budget(tokens, 1)
            error_console.print(f"Query embedding tokens: {tokens}")
            error_console.print(f"Estimated cost: ${tokens * pipeline.price / 1_000_000:.6f}")
            if typer.confirm("Send search query for remote embedding?", default=False, err=True):
                semantic = VectorRetriever(store, pipeline, approved=True)
            else:
                reason = "Semantic query was not approved; using keyword results"
    except Exception as exc:
        if strict:
            raise
        reason = (
            f"Semantic backend unavailable ({type(exc).__name__}: {exc}); "
            "using keyword results"
        )
    return semantic, reason


def _key_value_filters(values: list[str] | None, *, json_value: bool) -> dict:
    parsed = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter("Expected key=value")
        if json_value:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
            if isinstance(value, (dict, list)):
                raise typer.BadParameter("Only scalar metadata values are supported")
        parsed[key.strip()] = value
    return parsed


def _modified_filter(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("Use an ISO-8601 date or datetime") from exc


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
    database_path = (
        settings.index.database or Path.home() / ".obsai" / "index.db"
    ).expanduser()
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


@index_app.command("embeddings")
def index_embeddings() -> None:
    """Preflight and populate a generation-isolated local vector index."""
    from obsai.storage import Database

    settings = load_settings()
    database_path = (
        settings.index.database or Path.home() / ".obsai" / "index.db"
    ).expanduser()
    if not database_path.is_file():
        raise ConfigError("Index does not exist; run 'obsai index update' first")
    with Database(database_path) as database:
        _, pipeline = _embedding_pipeline(database)
        plan = pipeline.plan()
        console.print(f"Chunks requiring embeddings: {plan.chunks_requiring_embeddings}")
        console.print(f"Cache hits: {plan.cache_hits}")
        console.print(f"Estimated tokens: {plan.estimated_tokens}")
        console.print(f"Estimated requests: {plan.request_count}")
        console.print(f"Estimated cost: ${plan.estimated_cost_usd:.6f}")
        if plan.request_count and not typer.confirm("Proceed with remote embeddings?", default=False):
            raise typer.Exit(1)
        attached = asyncio.run(pipeline.execute(plan, approved=bool(plan.request_count)))
        console.print(f"Vectors attached: {attached}")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search text or natural-language question.")],
    mode: Annotated[str, typer.Option("--mode", help="Search mode: hybrid, keyword, or semantic.")] = "hybrid",
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum results.")] = 10,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Require a tag; repeatable.")] = None,
    folder: Annotated[str | None, typer.Option("--folder", help="Vault folder prefix.")] = None,
    modified_after: Annotated[str | None, typer.Option("--modified-after", help="Modified at or after ISO-8601 time.")] = None,
    modified_before: Annotated[str | None, typer.Option("--modified-before", help="Modified at or before ISO-8601 time.")] = None,
    frontmatter: Annotated[list[str] | None, typer.Option("--frontmatter", help="Top-level scalar key=value; repeatable.")] = None,
    dataview: Annotated[list[str] | None, typer.Option("--dataview", help="Inline field key=value; repeatable.")] = None,
    strict_semantic: Annotated[bool, typer.Option("--strict-semantic", help="Fail if semantic retrieval is unavailable.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print structured JSON.")] = False,
) -> None:
    """Search the local hybrid, keyword, or semantic index."""
    from obsai.retrieval import FTSRetriever, HybridRetriever, SearchFilters
    from obsai.storage import Database

    if mode not in ("hybrid", "keyword", "semantic"):
        raise typer.BadParameter("Use hybrid, keyword, or semantic", param_hint="--mode")
    settings = load_settings()
    database_path = (
        settings.index.database or Path.home() / ".obsai" / "index.db"
    ).expanduser()
    if not database_path.is_file():
        raise ConfigError("Index does not exist; run 'obsai index update' first")
    with Database(database_path) as database:
        try:
            filters = SearchFilters(
                tags=tuple(tag or ()), folder=folder,
                modified_after=_modified_filter(modified_after),
                modified_before=_modified_filter(modified_before),
                frontmatter=_key_value_filters(frontmatter, json_value=True),
                dataview=_key_value_filters(dataview, json_value=False),
            )
        except ValidationError as exc:
            raise typer.BadParameter(f"Invalid metadata filter: {exc}") from exc
        if mode == "keyword":
            results = FTSRetriever(database).search(query, limit=limit, filters=filters)
        else:
            semantic, reason = _semantic_retriever(
                database, query, strict=mode == "semantic" or strict_semantic
            )
            if mode == "semantic":
                if semantic is None:
                    raise ConfigError(reason)
                results = semantic.search(query, limit=limit, filters=filters)
            else:
                hybrid = HybridRetriever(
                    FTSRetriever(database), semantic,
                    on_semantic_failure="strict" if strict_semantic else "warn",
                    semantic_unavailable_reason=reason,
                )
                outcome = hybrid.search_with_status(query, limit=limit, filters=filters)
                results = list(outcome.results)
                for warning in outcome.warnings:
                    error_console.print(f"Warning: {warning}")
    if json_output:
        typer.echo(json.dumps([result.model_dump() for result in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            console.print(f"{result.title}  [{result.path}]", markup=False)
            if result.heading_path:
                console.print(" > ".join(result.heading_path))
            console.print(result.snippet, markup=False)


@app.command()
def ask(
    query: Annotated[str, typer.Argument(help="Question about indexed Vault notes.")],
) -> None:
    """Answer once from bounded retrieved evidence with validated citations."""
    from obsai.answering.context import ContextBuilder
    from obsai.answering.openai_provider import OpenAILLMProvider
    from obsai.answering.service import AskService
    from obsai.retrieval import FTSRetriever, HybridRetriever
    from obsai.storage import Database, SQLiteEvidenceRepository

    settings = load_settings()
    database_path = (
        settings.index.database or Path.home() / ".obsai" / "index.db"
    ).expanduser()
    if not database_path.is_file():
        raise ConfigError("Index does not exist; run 'obsai index update' first")
    with Database(database_path) as database:
        semantic, reason = _semantic_retriever(database, query)
        retriever = HybridRetriever(
            FTSRetriever(database), semantic, semantic_unavailable_reason=reason
        )
        service = AskService(
            retriever, ContextBuilder(SQLiteEvidenceRepository(database), settings.ask),
            OpenAILLMProvider(settings.ask), settings.ask,
        )
        answer = service.ask(query)
    for warning in answer.warnings:
        error_console.print(f"Warning: {warning}")
    console.print(answer.text, markup=False)
    if answer.sources:
        console.print("\nSources:")
        for source in answer.sources:
            record = source.record
            location = record.path
            if record.heading_path:
                location += " > " + " > ".join(record.heading_path)
            if record.block_id:
                location += f" ^{record.block_id}"
            console.print(f"[{source.citation_id}] {location}", markup=False)


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
