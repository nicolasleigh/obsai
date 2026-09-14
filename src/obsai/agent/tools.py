"""Agent tool boundary. All Vault mutations pass through TransactionService."""

import base64
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rich.console import Console

from obsai.agent.store import ArtifactStore
from obsai.retrieval.models import Retriever
from obsai.storage import Database, IndexRepository
from obsai.transactions import TransactionService
from obsai.transactions.models import TransactionOperation, TransactionPlan
from obsai.safe_write.models import FileChange


READ_TOOLS = frozenset({"search_notes", "read_note", "get_backlinks", "get_outgoing_links"})
WRITE_TOOLS = frozenset({"create_note", "update_note", "move_note", "trash_note", "update_frontmatter"})
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS


def _encode_bytes(values: dict[str, bytes | None]) -> dict[str, str | None]:
    return {key: base64.b64encode(value).decode("ascii") if value is not None else None
            for key, value in values.items()}


def _decode_bytes(values: dict[str, str | None]) -> dict[str, bytes | None]:
    return {key: base64.b64decode(value) if value is not None else None
            for key, value in values.items()}


def _serialize_plan(plan: TransactionPlan) -> dict[str, Any]:
    return {
        "vault_root": plan.vault_root,
        "operations": [asdict(item) for item in plan.operations],
        "changes": [asdict(item) for item in plan.changes],
        "originals": _encode_bytes(plan.originals),
        "finals": _encode_bytes(plan.finals),
        "original_modes": plan.original_modes,
        "absent_directories": plan.absent_directories,
        "ambiguous_backlinks": plan.ambiguous_backlinks,
    }


def _deserialize_plan(value: dict[str, Any]) -> TransactionPlan:
    return TransactionPlan(
        value["vault_root"],
        tuple(TransactionOperation(**item) for item in value["operations"]),
        tuple(FileChange(**{**item, "affected_backlinks": tuple(item["affected_backlinks"])})
              for item in value["changes"]),
        _decode_bytes(value["originals"]), _decode_bytes(value["finals"]),
        value["original_modes"], tuple(value["absent_directories"]),
        tuple(value["ambiguous_backlinks"]),
    )


class AgentTools:
    def __init__(self, database_path: Path, vault_root: Path, retriever: Retriever,
                 artifacts: ArtifactStore):
        self.database_path = database_path
        self.vault_root = vault_root
        self.retriever = retriever
        self.artifacts = artifacts

    def read(self, name: str, args: dict[str, Any]) -> tuple[str, list[str], list[str], str]:
        if name not in READ_TOOLS:
            raise ValueError(f"Unknown read tool: {name}")
        if name == "search_notes":
            results = self.retriever.search(str(args["query"]), limit=min(int(args.get("limit", 10)), 20))
            payload = [result.model_dump(mode="json") for result in results]
            ref = self.artifacts.put(payload)
            summary = "; ".join(
                f"note_id={item.note_id} chunk_id={item.chunk_id} {item.title} ({item.path})"
                for item in results[:5]
            ) or "No results"
            return ref, [item.note_id for item in results], [item.chunk_id for item in results], summary
        with Database(self.database_path) as database:
            repository = IndexRepository(database)
            note_id = str(args["note_id"])
            note = repository.notes.get(note_id)
            if note is None:
                raise KeyError(f"Unknown note ID: {note_id}")
            if name == "read_note":
                parsed = repository.notes.get_parsed(note_id)
                payload = parsed.model_dump(mode="json") if parsed is not None else {}
                summary = f"Read {note.path}: {str(payload.get('raw_content', ''))[:500]}"
            elif name == "get_backlinks":
                payload = [asdict(item) for item in repository.backlinks_for_path(note_id, note.path)]
                summary = f"{len(payload)} backlinks to {note.path}"
            else:
                payload = [asdict(item) for item in repository.links_for_note(note_id)]
                summary = f"{len(payload)} outgoing links from {note.path}"
            return self.artifacts.put(payload), [note_id], [], summary

    def plan_write(self, name: str, args: dict[str, Any]) -> tuple[str, str]:
        if name not in WRITE_TOOLS:
            raise ValueError(f"Unknown write tool: {name}")
        service = TransactionService(self.vault_root, database_path=self.database_path)
        path = str(args["path"])
        if name == "create_note":
            plan = service.plan([TransactionOperation.create(path, str(args["content"]))])
        elif name == "update_note":
            plan = service.plan([TransactionOperation.replace(path, str(args["old"]), str(args["new"]))])
        elif name == "move_note":
            plan = service.plan_move_with_backlinks(path, str(args["destination"]))
        elif name == "trash_note":
            plan = service.plan([TransactionOperation.trash(path)])
        else:
            updates = args["updates"]
            if not isinstance(updates, dict):
                raise ValueError("updates must be a mapping")
            plan = service.plan([TransactionOperation.frontmatter(path, updates)])
        capture = Console(record=True, force_terminal=False, width=100)
        service.preview(plan, capture)
        preview = capture.export_text()
        ref = self.artifacts.put(_serialize_plan(plan))
        return ref, preview

    def apply_write(self, ref: str):
        service = TransactionService(self.vault_root, database_path=self.database_path)
        plan = _deserialize_plan(self.artifacts.get(ref))
        return service.execute(plan, approved=True)
