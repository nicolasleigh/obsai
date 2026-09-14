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
note_app = typer.Typer(help="Preview and approve safe single-note Vault changes.", no_args_is_help=True)
transaction_app = typer.Typer(help="Inspect and recover Vault transactions.", no_args_is_help=True)
app.add_typer(index_app, name="index")
app.add_typer(note_app, name="note")
app.add_typer(transaction_app, name="transaction")
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
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=version_callback, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Global CLI options and visible unfinished-journal notice."""
    if ctx.invoked_subcommand is None:
        return
    from obsai.errors import RecoveryRequiredError
    from obsai.transactions import TransactionService

    settings = load_settings()
    if settings.vault.path is None or not settings.vault.path.expanduser().is_dir():
        return
    try:
        journals = TransactionService.journals(settings.vault.path)
    except RecoveryRequiredError as exc:
        error_console.print(f"Recovery required: {exc}")
        return
    for journal in journals:
        if journal["status"] in ("prepared", "applying", "rolling_back", "recovery_required"):
            error_console.print(
                f"Recovery required: transaction {journal['id']} is {journal['status']}; "
                f"run 'obsai transaction recover {journal['id']}'"
            )
        elif journal["status"] in ("committed", "index_dirty"):
            error_console.print(
                f"Index recovery required: transaction {journal['id']} is {journal['status']}; "
                "run 'obsai index update'"
            )


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
    from obsai.transactions import TransactionService

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    vault = settings.vault.path.expanduser()
    transaction_service = TransactionService(vault)
    transaction_service.ensure_ready()
    database_path = (
        settings.index.database or Path.home() / ".obsai" / "index.db"
    ).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with Database(database_path) as database:
        result = IncrementalIndexer(IndexRepository(database)).update(vault)
    TransactionService(vault, database_path=database_path).clear_index_dirty()

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


def _safe_write_service():
    from obsai.safe_write import SafeWriteService
    from obsai.transactions import TransactionService

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    TransactionService(settings.vault.path).ensure_ready()
    return SafeWriteService(settings.vault.path)


def _approve_change(service, change) -> None:
    service.preview(change, console)
    count = len(change.file.affected_backlinks)
    question = (
        f"Apply move with {count} affected backlink note(s)?"
        if change.file.operation == "move" and count else "Apply this change?"
    )
    if not typer.confirm(question, default=False):
        console.print("Cancelled")
        return
    service.apply(change, approved=True)
    console.print("Applied")


@note_app.command("create")
def note_create(
    path: Annotated[str, typer.Argument(help="Vault-relative .md path.")],
    content: Annotated[str, typer.Option("--content", help="Initial Markdown content.")],
) -> None:
    """Preview and create one note."""
    service = _safe_write_service()
    _approve_change(service, service.create_note(path, content))


@note_app.command("update")
def note_update(
    path: Annotated[str, typer.Argument(help="Vault-relative .md path.")],
    old: Annotated[str, typer.Option("--old", help="Exact text to replace once.")],
    new: Annotated[str, typer.Option("--new", help="Replacement text.")],
) -> None:
    """Preview one exact-span note edit."""
    service = _safe_write_service()
    _approve_change(service, service.update_note(path, old, new))


@note_app.command("move")
def note_move(
    path: Annotated[str, typer.Argument(help="Existing Vault-relative .md path.")],
    destination: Annotated[str, typer.Argument(help="New Vault-relative .md path.")],
) -> None:
    """Preview an atomic batch move with explicit-path backlink rewrites."""
    from obsai.transactions import TransactionService

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    database_path = (
        settings.index.database or Path.home() / ".obsai" / "index.db"
    ).expanduser()
    service = TransactionService(settings.vault.path, database_path=database_path)
    plan = service.plan_move_with_backlinks(path, destination)
    service.preview(plan, console)
    affected = len({p for change in plan.changes for p in change.affected_backlinks})
    if not typer.confirm(f"Apply move with {affected} affected backlink note(s)?", default=False):
        console.print("Cancelled")
        return
    result = service.execute(plan, approved=True)
    console.print("Applied")
    if result.index_dirty:
        error_console.print(f"Warning: {result.index_error}; run 'obsai index update'")


@note_app.command("trash")
def note_trash(path: Annotated[str, typer.Argument(help="Vault-relative .md path.")]) -> None:
    """Preview moving one note to the recoverable Vault trash."""
    service = _safe_write_service()
    _approve_change(service, service.trash_note(path))


@note_app.command("frontmatter")
def note_frontmatter(
    path: Annotated[str, typer.Argument(help="Vault-relative .md path.")],
    set_values: Annotated[list[str], typer.Option("--set", help="Set key=value; repeatable.")],
) -> None:
    """Preview structured YAML frontmatter updates."""
    import yaml

    updates = {}
    for item in set_values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter("Use key=value", param_hint="--set")
        parsed = yaml.safe_load(value)
        if isinstance(parsed, (dict, list)):
            raise typer.BadParameter("Use scalar frontmatter values", param_hint="--set")
        updates[key.strip()] = parsed
    service = _safe_write_service()
    _approve_change(service, service.update_frontmatter(path, updates))


@transaction_app.command("status")
def transaction_status() -> None:
    """Show pending recovery and index-dirty transaction journals."""
    from obsai.transactions import TransactionService

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    journals = TransactionService.journals(settings.vault.path)
    if not journals:
        console.print("No unfinished transactions")
    for journal in journals:
        console.print(f"{journal['id']}: {journal['status']}")
        for item in journal.get("originals", []):
            console.print(f"  {item['path']}", markup=False)


@transaction_app.command("recover")
def transaction_recover(
    transaction_id: Annotated[str, typer.Argument(help="Transaction ID from status.")],
) -> None:
    """Inspect a journal and confirm restoring its original Vault bytes."""
    from obsai.transactions import TransactionService
    from obsai.transactions.journal import TransactionJournal, UNFINISHED
    from obsai.errors import TransactionError

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    service = TransactionService(settings.vault.path)
    journal = TransactionJournal.load(service.root, transaction_id)
    console.print(f"Transaction {transaction_id}: {journal.data['status']}")
    if journal.data["status"] not in UNFINISHED:
        raise TransactionError("Vault transaction is committed; run 'obsai index update' to reconcile the index")
    console.print(f"Journal and backups: {journal.directory}", markup=False)
    for item in journal.data["originals"]:
        console.print(f"  {item['path']} (snapshot: {item['snapshot'] or 'absent'})", markup=False)
    service.preview_recovery(transaction_id, console)
    if not typer.confirm("Restore original Vault files from this journal?", default=False):
        console.print("Cancelled")
        return
    service.recover(transaction_id, approved=True)
    console.print("Recovered")


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
