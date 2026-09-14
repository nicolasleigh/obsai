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
agent_app = typer.Typer(help="Run or resume the bounded agent workflow.", no_args_is_help=True)
organize_app = typer.Typer(help="Review and apply conservative Vault organization proposals.", no_args_is_help=True)
links_app = typer.Typer(help="Inspect WikiLink graph and suggest related notes.", no_args_is_help=True)
app.add_typer(index_app, name="index")
app.add_typer(note_app, name="note")
app.add_typer(transaction_app, name="transaction")
app.add_typer(agent_app, name="agent")
app.add_typer(organize_app, name="organize")
app.add_typer(links_app, name="links")
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
    reason = "Semantic index missing; run 'obsai index embeddings'"
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
                reason = "Semantic query was not approved"
    except Exception as exc:
        if strict:
            raise
        reason = f"Semantic backend unavailable ({type(exc).__name__}: {exc})"
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
    database_path = (settings.index.database or Path.home() / ".obsai" / "index.db").expanduser()
    if database_path.with_name(database_path.name + ".building").exists():
        error_console.print("Interrupted shadow index found; run 'obsai index rebuild' to replace it safely")
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


@index_app.command("rebuild")
def index_rebuild() -> None:
    """Build and validate index.db.building, then atomically replace index.db."""
    from obsai.indexing import ShadowIndexRebuilder

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    database_path = (settings.index.database or Path.home() / ".obsai" / "index.db").expanduser()
    result = ShadowIndexRebuilder(settings.vault.path, database_path).rebuild()
    console.print(f"Shadow index validated and activated: {database_path}", markup=False)
    console.print(f"Notes indexed: {result.count('created')}")
    console.print("Embeddings must be regenerated with 'obsai index embeddings'")


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
        from obsai.shutdown import defer_shutdown

        # Signal handlers mark cancellation while an async request is in flight;
        # workers stop before scheduling the next batch.
        with defer_shutdown():
            attached = asyncio.run(pipeline.execute(plan, approved=bool(plan.request_count)))
        console.print(f"Vectors attached: {attached}")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search text or natural-language question.")],
    mode: Annotated[str, typer.Option("--mode", help="Search mode: hybrid, keyword, semantic, or graph.")] = "hybrid",
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
    """Search the local hybrid, keyword, semantic, or graph-expanded index."""
    from obsai.retrieval import FTSRetriever, HybridRetriever, SearchFilters
    from obsai.storage import Database

    if mode not in ("hybrid", "keyword", "semantic", "graph"):
        raise typer.BadParameter("Use hybrid, keyword, semantic, or graph", param_hint="--mode")
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
            elif mode == "hybrid":
                hybrid = HybridRetriever(
                    FTSRetriever(database), semantic,
                    on_semantic_failure="strict" if strict_semantic else "warn",
                    semantic_unavailable_reason=reason,
                )
                outcome = hybrid.search_with_status(query, limit=limit, filters=filters)
                results = list(outcome.results)
                for warning in outcome.warnings:
                    error_console.print(f"Warning: {warning}")
            else:
                from obsai.graph import GraphRepository, GraphService
                from obsai.retrieval import GraphRetriever
                from obsai.storage import IndexRepository

                hybrid = HybridRetriever(
                    FTSRetriever(database), semantic,
                    on_semantic_failure="strict" if strict_semantic else "warn",
                    semantic_unavailable_reason=reason,
                )
                repository = IndexRepository(database)
                graph = GraphService(repository, GraphRepository(database))
                retriever = GraphRetriever(hybrid, graph, repository)
                results = retriever.search(query, limit=limit, filters=filters)
                for warning in retriever.last_warnings:
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


def _agent_runtime(query: str):
    """Open durable workflow state separately from the Vault transaction journal."""
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    from obsai.agent.openai_planner import OpenAIDecisionProvider
    from obsai.agent.store import ArtifactStore
    from obsai.agent.tools import AgentTools
    from obsai.agent.workflow import AgentWorkflow
    from obsai.answering.context import ContextBuilder
    from obsai.answering.openai_provider import OpenAILLMProvider
    from obsai.answering.service import AskService
    from obsai.retrieval import FTSRetriever, HybridRetriever
    from obsai.storage import Database, SQLiteEvidenceRepository

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    database_path = (settings.index.database or Path.home() / ".obsai" / "index.db").expanduser()
    if not database_path.is_file():
        raise ConfigError("Index does not exist; run 'obsai index update' first")
    database = Database(database_path)
    artifacts = None
    checkpoint_connection = None
    try:
        semantic, reason = _semantic_retriever(database, query)
        retriever = HybridRetriever(FTSRetriever(database), semantic,
                                    semantic_unavailable_reason=reason)
        artifacts = ArtifactStore(database_path.with_name("agent-artifacts.db"))
        checkpoint_connection = sqlite3.connect(database_path.with_name("agent-checkpoints.db"),
                                                check_same_thread=False)
        checkpointer = SqliteSaver(checkpoint_connection)
        checkpointer.setup()
        answering = AskService(retriever, ContextBuilder(SQLiteEvidenceRepository(database), settings.ask),
                               OpenAILLMProvider(settings.ask), settings.ask)

        def answer_question(question: str):
            answer = answering.ask(question)
            return answer.text, [source.record.note_id for source in answer.sources], [
                source.record.chunk_id for source in answer.sources]

        workflow = AgentWorkflow(
            AgentTools(database_path, settings.vault.path, retriever, artifacts),
            artifacts, OpenAIDecisionProvider(settings.ask), checkpointer=checkpointer,
            answer_question=answer_question,
        )
        return workflow, database, artifacts, checkpoint_connection
    except BaseException:
        if checkpoint_connection is not None:
            checkpoint_connection.close()
        if artifacts is not None:
            artifacts.close()
        database.close()
        raise


@agent_app.command("run")
def agent_run(
    query: Annotated[str, typer.Argument(help="Request for the bounded agent.")],
    thread_id: Annotated[str | None, typer.Option("--thread-id", help="Stable workflow ID for resuming.")] = None,
) -> None:
    """Run until an answer or an approval checkpoint."""
    from uuid import uuid4

    identifier = thread_id or uuid4().hex
    workflow, database, artifacts, connection = _agent_runtime(query)
    try:
        result = workflow.run(query, identifier)
        _show_agent_result(identifier, result, artifacts)
    finally:
        database.close()
        artifacts.close()
        connection.close()


@agent_app.command("resume")
def agent_resume(thread_id: Annotated[str, typer.Argument(help="Workflow ID awaiting approval.")]) -> None:
    """Review a pending write preview, then approve or reject it."""
    workflow, database, artifacts, connection = _agent_runtime("")
    try:
        snapshot = workflow.graph.get_state({"configurable": {"thread_id": thread_id}})
        if not snapshot.tasks or not snapshot.tasks[0].interrupts:
            raise ConfigError(f"No pending approval for workflow {thread_id}")
        payload = snapshot.tasks[0].interrupts[0].value
        if payload.get("kind") != "write_approval":
            raise ConfigError("Workflow is not awaiting write approval")
        console.print(artifacts.get(payload["preview_ref"])["preview"], markup=False)
        approved = typer.confirm("Apply this Vault change?", default=False)
        result = workflow.resume(thread_id, approved=approved)
        _show_agent_result(thread_id, result, artifacts)
    finally:
        database.close()
        artifacts.close()
        connection.close()


def _show_agent_result(thread_id: str, result: dict, artifacts) -> None:
    if result.get("__interrupt__"):
        preview_ref = result["__interrupt__"][0].value["preview_ref"]
        console.print(artifacts.get(preview_ref)["preview"], markup=False)
        console.print(f"Approval required. Resume with: obsai agent resume {thread_id}")
    else:
        console.print(result.get("final_answer", "Agent stopped without an answer"), markup=False)


def _organizer_diff(organizer, plan) -> None:
    """Show a requested diff in pages; initial listing remains compact."""
    from io import StringIO

    output = StringIO()
    preview_console = Console(file=output, width=100, force_terminal=False)
    organizer.transaction.preview(plan, preview_console)
    lines = output.getvalue().splitlines()
    for start in range(0, len(lines), 200):
        if start and not typer.confirm("Show the next 200 diff lines?", default=False):
            console.print(f"Diff truncated after {start} of {len(lines)} lines")
            break
        console.print("\n".join(lines[start:start + 200]), markup=False)


def _proposal_numbers(value: str, count: int) -> list[int]:
    try:
        numbers = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise typer.BadParameter("Use comma-separated proposal numbers") from exc
    if not numbers or any(number < 1 or number > count for number in numbers):
        raise typer.BadParameter("Choose valid proposal numbers")
    return list(dict.fromkeys(numbers))


def _links_database_path() -> Path:
    settings = load_settings()
    database_path = (settings.index.database or Path.home() / ".obsai" / "index.db").expanduser()
    if not database_path.is_file():
        raise ConfigError("Index does not exist; run 'obsai index update' first")
    return database_path


@links_app.command("backlinks")
def links_backlinks(note: Annotated[str, typer.Argument(help="Indexed note path or ID.")]) -> None:
    """Show resolved inbound WikiLinks, including heading and block targets."""
    from obsai.graph import GraphRepository, GraphService
    from obsai.storage import Database, IndexRepository

    with Database(_links_database_path()) as database:
        graph = GraphService(IndexRepository(database), GraphRepository(database))
        links = graph.get_backlinks(note)
        for edge in links:
            target = edge.target_path or "(this note)"
            fragment = f"#^{edge.target_block_id}" if edge.target_block_id else (
                f"#{edge.target_heading}" if edge.target_heading else ""
            )
            console.print(f"{edge.source_path} → {target}{fragment}", markup=False)
        if not links:
            console.print("No backlinks")


@links_app.command("outgoing")
def links_outgoing(note: Annotated[str, typer.Argument(help="Indexed note path or ID.")]) -> None:
    """Show outbound WikiLinks and visibly mark unresolved targets."""
    from obsai.graph import GraphRepository, GraphService
    from obsai.storage import Database, IndexRepository

    with Database(_links_database_path()) as database:
        graph = GraphService(IndexRepository(database), GraphRepository(database))
        links = graph.get_outgoing_links(note)
        for edge in links:
            target = edge.target_path or "(this note)"
            fragment = f"#^{edge.target_block_id}" if edge.target_block_id else (
                f"#{edge.target_heading}" if edge.target_heading else ""
            )
            status = " [unresolved]" if edge.broken else ""
            console.print(f"{target}{fragment}{status}", markup=False)
        if not links:
            console.print("No outgoing links")


@links_app.command("related")
def links_related(
    note: Annotated[str, typer.Argument(help="Indexed note path or ID.")],
    depth: Annotated[int, typer.Option("--depth", min=0, max=2)] = 2,
    max_nodes: Annotated[int, typer.Option("--max-nodes", min=1)] = 50,
    max_edges: Annotated[int, typer.Option("--max-edges", min=0)] = 200,
) -> None:
    """Explore bidirectional WikiLink neighbors under explicit graph limits."""
    from obsai.graph import GraphLimits, GraphRepository, GraphService
    from obsai.storage import Database, IndexRepository

    with Database(_links_database_path()) as database:
        graph = GraphService(
            IndexRepository(database), GraphRepository(database),
            limits=GraphLimits(max_depth=2, max_nodes=max_nodes, max_edges=max_edges),
        )
        neighborhood = graph.get_neighbors(note, depth=depth)
        for node in neighborhood.nodes:
            console.print(f"Depth {node.distance}: {node.path} ({node.title})", markup=False)
        if neighborhood.truncated:
            console.print("Graph limit reached; results truncated")


@links_app.command("suggest")
def links_suggest(
    path: Annotated[str, typer.Argument(help="Vault-relative Markdown note path.")],
    limit: Annotated[int, typer.Option("--limit", min=1)] = 10,
    apply: Annotated[bool, typer.Option("--apply", help="Select, preview, and confirm links to append.")] = False,
) -> None:
    """Suggest semantic and graph-related notes without writing by default."""
    from obsai.graph import GraphRepository, GraphService, LinkSuggester
    from obsai.errors import ConflictError
    from obsai.safe_write import SafeWriteService
    from obsai.storage import Database, IndexRepository
    from obsai.transactions import TransactionOperation, TransactionService
    from obsai.vault.parser import parse_note

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    database_path = _links_database_path()
    vault = settings.vault.path.expanduser()
    with Database(database_path) as database:
        safe = SafeWriteService(vault)
        parsed = parse_note(safe._path(path), vault_root=safe.root)
        query = (parsed.title + "\n" + parsed.plain_text[:2000]).strip()
        semantic, reason = _semantic_retriever(database, query, strict=True)
        if semantic is None:
            raise ConfigError(reason)
        if parse_note(safe._path(path), vault_root=safe.root).raw_content != parsed.raw_content:
            raise ConflictError("Note changed while preparing link suggestions; rerun the command")
        repository = IndexRepository(database)
        graph = GraphService(repository, GraphRepository(database))
        suggestions = LinkSuggester(vault, repository, graph, semantic).suggest(path, limit=limit)
        if not suggestions:
            console.print("No new link suggestions")
            return
        for number, item in enumerate(suggestions, start=1):
            console.print(f"[{number}] {item.wikilink}  {item.title}  score={item.score:.3f}", markup=False)
            console.print(f"    {item.reason}", markup=False)
        if not apply:
            return
        selected = _proposal_numbers(typer.prompt("Suggestion numbers (comma-separated)"), len(suggestions))
        if parse_note(safe._path(path), vault_root=safe.root).raw_content != parsed.raw_content:
            raise ConflictError("Note changed since suggestions were shown; rerun the command")
        suffix = "\n\nRelated:\n" + "".join(
            f"- {suggestions[number - 1].wikilink}\n" for number in selected
        )
        transaction = TransactionService(vault, database_path=database_path)
        plan = transaction.plan([TransactionOperation.append(path, suffix)])
        transaction.preview(plan, console)
        if not typer.confirm("Apply selected links?", default=False):
            console.print("Cancelled")
            return
        result = transaction.execute(plan, approved=True)
        console.print("Applied" if result.committed else "Cancelled")
        if result.index_dirty:
            error_console.print(f"Warning: {result.index_error}; run 'obsai index update'")


@organize_app.command("inbox")
def organize_inbox() -> None:
    """Scan Inbox, propose conservative destinations, then request approval."""
    from obsai.organizer import InboxOrganizer
    from obsai.retrieval import FTSRetriever
    from obsai.storage import Database, IndexRepository

    settings = load_settings()
    if settings.vault.path is None:
        raise ConfigError("No vault configured; set vault.path in config.toml")
    database_path = (settings.index.database or Path.home() / ".obsai" / "index.db").expanduser()
    if not database_path.is_file():
        raise ConfigError("Index does not exist; run 'obsai index update' first")
    with Database(database_path) as database:
        organizer = InboxOrganizer(settings.vault.path, database_path, IndexRepository(database),
                                   FTSRetriever(database), inbox=settings.organize.inbox)
        proposals = organizer.propose()
        if not proposals:
            console.print(f"{settings.organize.inbox} is empty")
            return
        default_numbers = [number for number, proposal in enumerate(proposals, start=1)
                           if proposal.selected_by_default]
        console.print(
            f"Inbox proposals: {len(proposals)} note(s), {len(default_numbers)} default selected, "
            f"{sum(bool(item.issue) for item in proposals)} conflict(s)"
        )
        shown_count = 0
        for number, proposal in enumerate(proposals, start=1):
            if number > 1 and (number - 1) % 20 == 0:
                if not typer.confirm("Show the next 20 proposals?", default=False):
                    console.print(f"{len(proposals) - shown_count} more proposal(s) hidden; use Select or View diff by number")
                    break
            marker = "*" if proposal.selected_by_default else " "
            console.print(f"[{number}{marker}] {proposal.path}", markup=False)
            console.print(f"  Move: {proposal.destination or 'Leave in Inbox'}", markup=False)
            console.print(f"  Title: {proposal.title}", markup=False)
            console.print(
                "  Tags: " + (", ".join(f"+ #{tag}" for tag in proposal.add_tags) or "—"),
                markup=False,
            )
            console.print(
                "  Links: " + (", ".join(f"+ {link.wikilink}" for link in proposal.add_links) or "—"),
                markup=False,
            )
            console.print(
                f"  Affected backlinks: {len(proposal.affected_backlinks)}"
                f"  Confidence: {proposal.confidence:.0%}", markup=False,
            )
            console.print(f"  Reason: {proposal.issue or proposal.reason}", markup=False)
            shown_count += 1
        console.print("* = included by Apply all; low-confidence and unsafe proposals stay unselected")
        while True:
            choice = typer.prompt("[a] Apply all  [s] Select  [v] View diff  [q] Cancel", default="q").strip().lower()
            if choice == "q":
                console.print("Cancelled")
                return
            if choice not in {"a", "s", "v"}:
                console.print("Choose a, s, v, or q")
                continue
            if choice == "a":
                numbers = default_numbers
            else:
                entered = typer.prompt(
                    "Proposal numbers (comma-separated)",
                    default=",".join(map(str, default_numbers)),
                )
                numbers = _proposal_numbers(entered, len(proposals))
            if not numbers:
                console.print("No safe proposals selected")
                continue
            plan = organizer.plan(proposals, numbers)
            if choice == "v":
                _organizer_diff(organizer, plan)
                continue
            console.print(f"One transaction: {len(numbers)} note(s), {len(plan.changes)} file change(s)")
            if choice == "a" and shown_count < len(proposals) and not typer.confirm(
                f"Apply {len(numbers)} default-selected notes, including proposals not displayed?",
                default=False,
            ):
                console.print("Cancelled")
                return
            if choice == "s" and not typer.confirm("Apply selected changes?", default=False):
                console.print("Cancelled")
                return
            result = organizer.apply(plan)
            if result.committed:
                console.print(f"Applied and verified {len(numbers)} note(s)")
            if result.index_dirty:
                error_console.print(f"Warning: {result.index_error}; run 'obsai index update'")
            return


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
    from obsai.shutdown import ShutdownController, ShutdownRequested

    configure_logging()
    try:
        with ShutdownController():
            app()
    except ShutdownRequested as exc:
        error_console.print("Shutdown requested; current operation stopped safely")
        raise SystemExit(exc.exit_code) from None
    except ObsAIError as exc:
        logger.debug("CLI error: %s", exc)
        error_console.print(f"Error: {exc}")
        raise SystemExit(2) from exc
    except Exception:
        logger.exception("Unexpected CLI error")
        error_console.print("Error: Unexpected failure")
        raise SystemExit(1) from None
    finally:
        logging.shutdown()
