"""Typer entry point: parse arguments, delegate to the application layer, render.

Everything that decides *what* happens lives in ``obsai.application``. Everything
here decides *how it looks in a terminal*. Three rules keep that split honest:

- no command derives a path, assembles a domain service or interprets a domain
  object; it asks ``application`` for a value and draws it;
- approval stays here. A prompt is a terminal concept — the HTTP adapter asks the
  same question with a dialog — so ``typer.confirm`` never moves down;
- rendering is explicit. ``DiffLine`` carries a semantic style tag, and the only
  place that turns ``added`` back into green is :func:`_render_diff`.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console

from obsai import __version__
from obsai.application.paths import (
    database_path,
    require_index,
    require_vault,
)
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

#: Semantic diff tags back to Rich styles. The application layer never names a
#: colour, so this is the single place the two vocabularies meet.
DIFF_STYLES = {"added": "green", "removed": "red", "hunk": "cyan", "notice": "yellow"}


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render_diff(target: Console, lines) -> None:
    """Draw structured diff lines exactly as the domain preview used to print them."""
    for line in lines:
        target.print(
            line.text,
            style=DIFF_STYLES.get(line.style),
            markup=False,
            highlight=line.highlight,
        )


def _render_results(results) -> None:
    for result in results:
        console.print(f"{result.title}  [{result.path}]", markup=False)
        if result.heading_path:
            console.print(" > ".join(result.heading_path))
        console.print(result.snippet, markup=False)


def _paged_diff(view) -> None:
    """Show a requested diff in pages; initial listing remains compact."""
    from io import StringIO

    output = StringIO()
    preview_console = Console(file=output, width=100, force_terminal=False)
    _render_diff(preview_console, view.diff)
    lines = output.getvalue().splitlines()
    for start in range(0, len(lines), 200):
        if start and not typer.confirm("Show the next 200 diff lines?", default=False):
            console.print(f"Diff truncated after {start} of {len(lines)} lines")
            break
        console.print("\n".join(lines[start:start + 200]), markup=False)


# --------------------------------------------------------------------------- #
# Shared interaction
# --------------------------------------------------------------------------- #


def _write_lock(settings, operation: str):
    """Serialise a commit against every other writer in the process or on the host."""
    from obsai.application.locks import vault_lock

    return vault_lock(database_path(settings), operation=operation)


def _approve_remote(settings, database, query: str, *, strict: bool = False):
    """Probe the semantic backend and, if a query would leave the machine, ask.

    Returns ``(probe, approval)``. Token and cost estimates go to stderr so they
    cannot be mistaken for results. ``strict`` re-raises instead of degrading,
    which is what ``--mode semantic`` and ``--strict-semantic`` require.
    """
    from obsai.application.search import approve_consent, probe_semantic

    probe = probe_semantic(query, database=database, settings=settings, strict=strict)
    if probe.consent is None:
        return probe, None
    consent = probe.consent
    error_console.print(f"Query embedding tokens: {consent.query_tokens}")
    error_console.print(f"Estimated cost: ${consent.estimated_cost_usd:.6f}")
    approved = typer.confirm("Send search query for remote embedding?", default=False, err=True)
    return probe, approve_consent(consent, approved=approved)


def _single_question(view) -> str:
    change = view.only_change
    count = len(change.affected_backlinks) if change is not None else 0
    if change is not None and change.operation == "move" and count:
        return f"Apply move with {count} affected backlink note(s)?"
    return "Apply this change?"


def _apply_plan(settings, view, question: str, operation: str) -> None:
    """Render a prepared plan, ask, then commit or drop it."""
    from obsai.application.changes import approve, discard

    _render_diff(console, view.diff)
    if not typer.confirm(question, default=False):
        discard(view)
        console.print("Cancelled")
        return
    with _write_lock(settings, operation):
        approve(view, nonce=view.nonce)
    console.print("Applied")


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


def _proposal_numbers(value: str, count: int) -> list[int]:
    try:
        numbers = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise typer.BadParameter("Use comma-separated proposal numbers") from exc
    if not numbers or any(number < 1 or number > count for number in numbers):
        raise typer.BadParameter("Choose valid proposal numbers")
    return list(dict.fromkeys(numbers))


def version_callback(value: bool) -> None:
    if value:
        console.print(f"obsai {__version__}")
        raise typer.Exit()


# --------------------------------------------------------------------------- #
# Root
# --------------------------------------------------------------------------- #


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
    from obsai.application.changes import journal_views
    from obsai.errors import RecoveryRequiredError
    from obsai.transactions import TransactionService

    settings = load_settings()
    index_path = database_path(settings)
    if index_path.with_name(index_path.name + ".building").exists():
        error_console.print("Interrupted shadow index found; run 'obsai index rebuild' to replace it safely")
    vault = settings.vault.path
    if vault is None or not vault.expanduser().is_dir():
        return
    try:
        journals = journal_views(TransactionService.journals(vault))
    except RecoveryRequiredError as exc:
        error_console.print(f"Recovery required: {exc}")
        return
    for journal in journals:
        if journal.unfinished:
            error_console.print(
                f"Recovery required: transaction {journal.transaction_id} is {journal.status}; "
                f"run 'obsai transaction recover {journal.transaction_id}'"
            )
        elif journal.needs_index_update:
            error_console.print(
                f"Index recovery required: transaction {journal.transaction_id} is {journal.status}; "
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


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #


@index_app.command("update")
def index_update() -> None:
    """Synchronize changed Vault notes into the SQLite metadata index."""
    from obsai.indexing import IncrementalIndexer
    from obsai.storage import Database, IndexRepository
    from obsai.transactions import TransactionService

    settings = load_settings()
    vault = require_vault(settings)
    index_path = database_path(settings)
    transaction_service = TransactionService(vault)
    transaction_service.ensure_ready()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock(settings, "index update"):
        with Database(index_path) as database:
            result = IncrementalIndexer(IndexRepository(database)).update(vault)
        TransactionService(vault, database_path=index_path).clear_index_dirty()

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
    vault = require_vault(settings)
    index_path = database_path(settings)
    # The rebuild's own lock only stops two rebuilds; this one also stops a note
    # write or an incremental update from racing the swap.
    with _write_lock(settings, "index rebuild"):
        result = ShadowIndexRebuilder(vault, index_path).rebuild()
    console.print(f"Shadow index validated and activated: {index_path}", markup=False)
    console.print(f"Notes indexed: {result.count('created')}")
    console.print("Embeddings must be regenerated with 'obsai index embeddings'")


@index_app.command("embeddings")
def index_embeddings() -> None:
    """Preflight and populate a generation-isolated local vector index."""
    from obsai.application.embedding import build_embedding_pipeline
    from obsai.storage import Database

    settings = load_settings()
    index_path = require_index(settings)
    with _write_lock(settings, "index embeddings"):
        with Database(index_path) as database:
            _, pipeline = build_embedding_pipeline(database, settings)
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


# --------------------------------------------------------------------------- #
# Search and answering
# --------------------------------------------------------------------------- #


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
    from obsai.application.dto import SearchRequest
    from obsai.application.search import search as run_search
    from obsai.retrieval import SearchFilters
    from obsai.storage import Database

    if mode not in ("hybrid", "keyword", "semantic", "graph"):
        raise typer.BadParameter("Use hybrid, keyword, semantic, or graph", param_hint="--mode")
    settings = load_settings()
    index_path = require_index(settings)
    with Database(index_path) as database:
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
        request = SearchRequest(
            query=query, mode=mode, limit=limit, filters=filters, strict_semantic=strict_semantic
        )
        probe = approval = None
        if request.requires_semantic:
            probe, approval = _approve_remote(
                settings, database, query, strict=request.semantic_is_mandatory
            )
        outcome = run_search(
            request, database=database, settings=settings, probe=probe, approval=approval
        )
    for warning in outcome.warnings:
        error_console.print(f"Warning: {warning}")
    if json_output:
        typer.echo(json.dumps(
            [result.model_dump() for result in outcome.results], ensure_ascii=False, indent=2
        ))
    else:
        _render_results(outcome.results)


@app.command()
def ask(
    query: Annotated[str, typer.Argument(help="Question about indexed Vault notes.")],
) -> None:
    """Answer once from bounded retrieved evidence with validated citations."""
    from obsai.application.answering import ask as run_ask
    from obsai.application.dto import AskRequest
    from obsai.storage import Database

    settings = load_settings()
    index_path = require_index(settings)
    with Database(index_path) as database:
        probe, approval = _approve_remote(settings, database, query)
        outcome = run_ask(
            AskRequest(query=query),
            database=database, settings=settings, probe=probe, approval=approval,
        )
    for warning in outcome.warnings:
        error_console.print(f"Warning: {warning}")
    console.print(outcome.text, markup=False)
    if outcome.citations:
        console.print("\nSources:")
        for citation in outcome.citations:
            console.print(f"[{citation.citation_id}] {citation.location}", markup=False)


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


def _agent_settings():
    """Vault and index locations for the agent, checked in the historical order."""
    settings = load_settings()
    require_vault(settings)
    return settings, require_index(settings)


@agent_app.command("run")
def agent_run(
    query: Annotated[str, typer.Argument(help="Request for the bounded agent.")],
    thread_id: Annotated[str | None, typer.Option("--thread-id", help="Stable workflow ID for resuming.")] = None,
) -> None:
    """Run until an answer or an approval checkpoint."""
    from uuid import uuid4

    from obsai.application.agent_runtime import build_runtime
    from obsai.storage import Database

    settings, index_path = _agent_settings()
    identifier = thread_id or uuid4().hex
    with Database(index_path) as probe_database:
        probe, approval = _approve_remote(settings, probe_database, query)
    with build_runtime(
        settings, index_path, probe=probe, approval=approval, query=query
    ) as runtime:
        result = runtime.workflow.run(query, identifier)
        _show_agent_result(identifier, result, runtime.artifacts)


@agent_app.command("resume")
def agent_resume(thread_id: Annotated[str, typer.Argument(help="Workflow ID awaiting approval.")]) -> None:
    """Review a pending write preview, then approve or reject it."""
    from obsai.application.agent_runtime import build_runtime
    from obsai.storage import Database

    settings, index_path = _agent_settings()
    with Database(index_path) as probe_database:
        probe, approval = _approve_remote(settings, probe_database, "")
    with build_runtime(settings, index_path, probe=probe, approval=approval) as runtime:
        snapshot = runtime.workflow.graph.get_state({"configurable": {"thread_id": thread_id}})
        if not snapshot.tasks or not snapshot.tasks[0].interrupts:
            raise ConfigError(f"No pending approval for workflow {thread_id}")
        payload = snapshot.tasks[0].interrupts[0].value
        if payload.get("kind") != "write_approval":
            raise ConfigError("Workflow is not awaiting write approval")
        console.print(runtime.artifacts.get(payload["preview_ref"])["preview"], markup=False)
        approved = typer.confirm("Apply this Vault change?", default=False)
        result = runtime.workflow.resume(thread_id, approved=approved)
        _show_agent_result(thread_id, result, runtime.artifacts)


def _show_agent_result(thread_id: str, result: dict, artifacts) -> None:
    if result.get("__interrupt__"):
        preview_ref = result["__interrupt__"][0].value["preview_ref"]
        console.print(artifacts.get(preview_ref)["preview"], markup=False)
        console.print(f"Approval required. Resume with: obsai agent resume {thread_id}")
    else:
        console.print(result.get("final_answer", "Agent stopped without an answer"), markup=False)


# --------------------------------------------------------------------------- #
# Organize
# --------------------------------------------------------------------------- #


@organize_app.command("inbox")
def organize_inbox() -> None:
    """Scan Inbox, propose conservative destinations, then request approval."""
    from obsai.application.organizer import apply as apply_selection
    from obsai.application.organizer import plan_selection, propose
    from obsai.storage import Database

    settings = load_settings()
    vault = require_vault(settings)
    index_path = require_index(settings)
    with Database(index_path) as database:
        scan = propose(vault, index_path, database, settings)
        preview = scan.preview
        if not preview.proposals:
            console.print(f"{preview.inbox} is empty")
            return
        default_numbers = list(preview.default_numbers)
        console.print(
            f"Inbox proposals: {len(preview.proposals)} note(s), {len(default_numbers)} default selected, "
            f"{preview.conflict_count} conflict(s)"
        )
        shown_count = 0
        for proposal in preview.proposals:
            if proposal.number > 1 and (proposal.number - 1) % 20 == 0:
                if not typer.confirm("Show the next 20 proposals?", default=False):
                    console.print(
                        f"{len(preview.proposals) - shown_count} more proposal(s) hidden; "
                        "use Select or View diff by number"
                    )
                    break
            marker = "*" if proposal.selected_by_default else " "
            console.print(f"[{proposal.number}{marker}] {proposal.path}", markup=False)
            console.print(f"  Move: {proposal.destination or 'Leave in Inbox'}", markup=False)
            console.print(f"  Title: {proposal.title}", markup=False)
            console.print(
                "  Tags: " + (", ".join(f"+ #{tag}" for tag in proposal.add_tags) or "—"),
                markup=False,
            )
            console.print(
                "  Links: " + (", ".join(f"+ {link}" for link in proposal.add_links) or "—"),
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
                numbers = _proposal_numbers(entered, len(preview.proposals))
            if not numbers:
                console.print("No safe proposals selected")
                continue
            view = plan_selection(scan, numbers)
            if choice == "v":
                _paged_diff(view)
                continue
            console.print(f"One transaction: {len(numbers)} note(s), {len(view.changes)} file change(s)")
            if choice == "a" and shown_count < len(preview.proposals) and not typer.confirm(
                f"Apply {len(numbers)} default-selected notes, including proposals not displayed?",
                default=False,
            ):
                console.print("Cancelled")
                return
            if choice == "s" and not typer.confirm("Apply selected changes?", default=False):
                console.print("Cancelled")
                return
            with _write_lock(settings, "organize inbox"):
                result = apply_selection(view, nonce=view.nonce)
            if result.committed:
                console.print(f"Applied and verified {len(numbers)} note(s)")
            if result.index_dirty:
                error_console.print(f"Warning: {result.index_error}; run 'obsai index update'")
            return


# --------------------------------------------------------------------------- #
# Note writes
# --------------------------------------------------------------------------- #


def _safe_write_service(settings):
    from obsai.safe_write import SafeWriteService
    from obsai.transactions import TransactionService

    vault = require_vault(settings)
    TransactionService(vault).ensure_ready()
    return SafeWriteService(vault)


@note_app.command("create")
def note_create(
    path: Annotated[str, typer.Argument(help="Vault-relative .md path.")],
    content: Annotated[str, typer.Option("--content", help="Initial Markdown content.")],
) -> None:
    """Preview and create one note."""
    from obsai.application.changes import plan_single

    settings = load_settings()
    service = _safe_write_service(settings)
    view = plan_single(service, service.create_note(path, content))
    _apply_plan(settings, view, _single_question(view), "note create")


@note_app.command("update")
def note_update(
    path: Annotated[str, typer.Argument(help="Vault-relative .md path.")],
    old: Annotated[str, typer.Option("--old", help="Exact text to replace once.")],
    new: Annotated[str, typer.Option("--new", help="Replacement text.")],
) -> None:
    """Preview one exact-span note edit."""
    from obsai.application.changes import plan_single

    settings = load_settings()
    service = _safe_write_service(settings)
    view = plan_single(service, service.update_note(path, old, new))
    _apply_plan(settings, view, _single_question(view), "note update")


@note_app.command("move")
def note_move(
    path: Annotated[str, typer.Argument(help="Existing Vault-relative .md path.")],
    destination: Annotated[str, typer.Argument(help="New Vault-relative .md path.")],
) -> None:
    """Preview an atomic batch move with explicit-path backlink rewrites."""
    from obsai.application.changes import approve, plan_transaction
    from obsai.transactions import TransactionService

    settings = load_settings()
    vault = require_vault(settings)
    service = TransactionService(vault, database_path=database_path(settings))
    view = plan_transaction(service, service.plan_move_with_backlinks(path, destination))
    _render_diff(console, view.diff)
    if not typer.confirm(
        f"Apply move with {view.affected_backlink_count} affected backlink note(s)?", default=False
    ):
        console.print("Cancelled")
        return
    with _write_lock(settings, "note move"):
        outcome = approve(view, nonce=view.nonce)
    console.print("Applied")
    if outcome.index_dirty:
        error_console.print(f"Warning: {outcome.index_error}; run 'obsai index update'")


@note_app.command("trash")
def note_trash(path: Annotated[str, typer.Argument(help="Vault-relative .md path.")]) -> None:
    """Preview moving one note to the recoverable Vault trash."""
    from obsai.application.changes import plan_single

    settings = load_settings()
    service = _safe_write_service(settings)
    view = plan_single(service, service.trash_note(path))
    _apply_plan(settings, view, _single_question(view), "note trash")


@note_app.command("frontmatter")
def note_frontmatter(
    path: Annotated[str, typer.Argument(help="Vault-relative .md path.")],
    set_values: Annotated[list[str], typer.Option("--set", help="Set key=value; repeatable.")],
) -> None:
    """Preview structured YAML frontmatter updates."""
    import yaml

    from obsai.application.changes import plan_single

    updates = {}
    for item in set_values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter("Use key=value", param_hint="--set")
        parsed = yaml.safe_load(value)
        if isinstance(parsed, (dict, list)):
            raise typer.BadParameter("Use scalar frontmatter values", param_hint="--set")
        updates[key.strip()] = parsed
    settings = load_settings()
    service = _safe_write_service(settings)
    view = plan_single(service, service.update_frontmatter(path, updates))
    _apply_plan(settings, view, _single_question(view), "note frontmatter")


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


@transaction_app.command("status")
def transaction_status() -> None:
    """Show pending recovery and index-dirty transaction journals."""
    from obsai.application.changes import journal_views
    from obsai.transactions import TransactionService

    settings = load_settings()
    vault = require_vault(settings)
    journals = journal_views(TransactionService.journals(vault))
    if not journals:
        console.print("No unfinished transactions")
    for journal in journals:
        console.print(f"{journal.transaction_id}: {journal.status}")
        for item in journal.originals:
            console.print(f"  {item.path}", markup=False)


@transaction_app.command("recover")
def transaction_recover(
    transaction_id: Annotated[str, typer.Argument(help="Transaction ID from status.")],
) -> None:
    """Inspect a journal and confirm restoring its original Vault bytes."""
    from obsai.application.changes import recovery_view
    from obsai.errors import TransactionError
    from obsai.transactions import TransactionService

    settings = load_settings()
    vault = require_vault(settings)
    service = TransactionService(vault)
    view = recovery_view(service, transaction_id)
    console.print(f"Transaction {transaction_id}: {view.status}")
    if not view.recoverable:
        raise TransactionError("Vault transaction is committed; run 'obsai index update' to reconcile the index")
    console.print(f"Journal and backups: {view.journal_directory}", markup=False)
    for item in view.originals:
        console.print(f"  {item.path} (snapshot: {item.snapshot or 'absent'})", markup=False)
    _render_diff(console, view.diff)
    if not typer.confirm("Restore original Vault files from this journal?", default=False):
        console.print("Cancelled")
        return
    with _write_lock(settings, "transaction recover"):
        service.recover(transaction_id, approved=True)
    console.print("Recovered")


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


def _links_database_path() -> Path:
    return require_index(load_settings())


def _link_target(edge) -> str:
    """``target#heading`` / ``target#^block``, or a placeholder for an unresolved link."""
    target = edge.target_path or "(this note)"
    if edge.target_block_id:
        return f"{target}#^{edge.target_block_id}"
    if edge.target_heading:
        return f"{target}#{edge.target_heading}"
    return target


def _graph_service(database, limits=None):
    from obsai.graph import GraphRepository, GraphService
    from obsai.storage import IndexRepository

    repository = IndexRepository(database)
    graph_repository = GraphRepository(database)
    if limits is None:
        return GraphService(repository, graph_repository)
    return GraphService(repository, graph_repository, limits=limits)


@links_app.command("backlinks")
def links_backlinks(note: Annotated[str, typer.Argument(help="Indexed note path or ID.")]) -> None:
    """Show resolved inbound WikiLinks, including heading and block targets."""
    from obsai.storage import Database

    with Database(_links_database_path()) as database:
        links = _graph_service(database).get_backlinks(note)
        for edge in links:
            console.print(f"{edge.source_path} → {_link_target(edge)}", markup=False)
        if not links:
            console.print("No backlinks")


@links_app.command("outgoing")
def links_outgoing(note: Annotated[str, typer.Argument(help="Indexed note path or ID.")]) -> None:
    """Show outbound WikiLinks and visibly mark unresolved targets."""
    from obsai.storage import Database

    with Database(_links_database_path()) as database:
        links = _graph_service(database).get_outgoing_links(note)
        for edge in links:
            status = " [unresolved]" if edge.broken else ""
            console.print(f"{_link_target(edge)}{status}", markup=False)
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
    from obsai.graph import GraphLimits
    from obsai.storage import Database

    with Database(_links_database_path()) as database:
        graph = _graph_service(
            database, GraphLimits(max_depth=2, max_nodes=max_nodes, max_edges=max_edges)
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
    from obsai.application.changes import approve, plan_batch
    from obsai.application.search import SEMANTIC_NOT_APPROVED, approval_covers, build_semantic_retriever
    from obsai.errors import ConflictError
    from obsai.graph import GraphRepository, GraphService, LinkSuggester
    from obsai.safe_write import SafeWriteService
    from obsai.storage import Database, IndexRepository
    from obsai.transactions import TransactionOperation, TransactionService
    from obsai.vault.parser import parse_note

    settings = load_settings()
    vault = require_vault(settings)
    index_path = require_index(settings)
    with Database(index_path) as database:
        safe = SafeWriteService(vault)
        parsed = parse_note(safe.path(path), vault_root=safe.root)
        query = (parsed.title + "\n" + parsed.plain_text[:2000]).strip()
        probe, approval = _approve_remote(settings, database, query, strict=True)
        if probe.consent is None:
            raise ConfigError(probe.reason)
        if approval is None or not approval_covers(approval, probe.consent):
            raise ConfigError(SEMANTIC_NOT_APPROVED)
        semantic = build_semantic_retriever(database, settings)
        if parse_note(safe.path(path), vault_root=safe.root).raw_content != parsed.raw_content:
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
        if parse_note(safe.path(path), vault_root=safe.root).raw_content != parsed.raw_content:
            raise ConflictError("Note changed since suggestions were shown; rerun the command")
        suffix = "\n\nRelated:\n" + "".join(
            f"- {suggestions[number - 1].wikilink}\n" for number in selected
        )
        transaction = TransactionService(vault, database_path=index_path)
        view = plan_batch(transaction, [TransactionOperation.append(path, suffix)])
        _render_diff(console, view.diff)
        if not typer.confirm("Apply selected links?", default=False):
            console.print("Cancelled")
            return
        with _write_lock(settings, "links suggest"):
            outcome = approve(view, nonce=view.nonce)
        console.print("Applied" if outcome.committed else "Cancelled")
        if outcome.index_dirty:
            error_console.print(f"Warning: {outcome.index_error}; run 'obsai index update'")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


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
