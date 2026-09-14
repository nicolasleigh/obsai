"""Conservative local Inbox classifier and one-approval/one-transaction planner."""

import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Sequence

from obsai.errors import CollisionError, ConfigError, TransactionError
from obsai.organizer.models import OrganizerProposal, RelatedLink
from obsai.retrieval.models import Retriever
from obsai.safe_write.service import SafeWriteService
from obsai.storage.repositories import IndexRepository
from obsai.transactions import TransactionService
from obsai.transactions.models import TransactionOperation, TransactionPlan, TransactionResult
from obsai.vault.parser import parse_note
from obsai.vault.scanner import scan_markdown_files
from obsai.shutdown import check_shutdown


def _terms(note) -> list[str]:
    values = [note.title, *note.tags, *(heading.text for heading in note.headings[:2])]
    terms = []
    for value in values:
        for word in re.findall(r"[^\W_]+", value.lower(), re.UNICODE):
            if len(word) >= 3 or any("\u3400" <= char <= "\u9fff" for char in word):
                if word not in terms:
                    terms.append(word)
    return terms[:8]


def _filename(note) -> str:
    stem = PurePosixPath(note.path).stem
    if note.title == stem:
        return PurePosixPath(note.path).name
    title = re.sub(r"[\\/\x00-\x1f:*?\"<>|]", "-", note.title).strip(" .")
    if not title or title in {".", ".."} or len(title) > 80:
        return PurePosixPath(note.path).name
    return f"{title}.md"


class InboxOrganizer:
    def __init__(self, vault_root: Path, database_path: Path, repository: IndexRepository,
                 retriever: Retriever, *, inbox: str = "Inbox"):
        self.vault = vault_root.expanduser().resolve(strict=True)
        self.database_path = database_path
        self.repository = repository
        self.retriever = retriever
        self.safe = SafeWriteService(self.vault)
        # Route through SafeWriteService path validation, including symlink checks.
        self.inbox = inbox.rstrip("/")
        self.safe._path(f"{self.inbox}/probe.md")
        self.transaction = TransactionService(self.vault, database_path=database_path)

    def propose(self) -> list[OrganizerProposal]:
        self.transaction.ensure_ready()
        inbox_root = self.vault / self.inbox
        if not inbox_root.is_dir() or inbox_root.is_symlink():
            raise ConfigError(f"Inbox directory does not exist: {self.inbox}")
        scanned = scan_markdown_files(self.vault)
        paths = [path for path in scanned
                 if path.relative_to(self.vault).as_posix().startswith(self.inbox + "/")]
        visible_paths = {path.relative_to(self.vault).as_posix()
                         for path in scanned}
        proposals = [self._propose_one(parse_note(path, vault_root=self.vault), visible_paths)
                     for path in paths]
        destinations = defaultdict(list)
        for index, proposal in enumerate(proposals):
            if proposal.destination:
                destinations[proposal.destination].append(index)
        for indices in destinations.values():
            if len(indices) > 1:
                for index in indices:
                    proposal = proposals[index]
                    proposals[index] = OrganizerProposal(
                        proposal.path, proposal.destination, proposal.title, proposal.add_tags,
                        proposal.add_links, proposal.affected_backlinks, proposal.confidence,
                        proposal.reason, "Multiple Inbox notes propose the same destination",
                    )
        return proposals

    def _propose_one(self, note, visible_paths: set[str]) -> OrganizerProposal:
        check_shutdown()
        evidence: dict[str, float] = defaultdict(float)
        for term in _terms(note):
            check_shutdown()
            try:
                results = self.retriever.search(term, limit=20)
            except Exception as exc:
                return OrganizerProposal(note.path, None, note.title, (), (), (), 0.0,
                                         f"Related-note retrieval failed: {type(exc).__name__}: {exc}")
            seen_for_term: set[str] = set()
            for result in results:
                if result.note_id in seen_for_term or result.path.startswith(self.inbox + "/"):
                    continue
                seen_for_term.add(result.note_id)
                if result.path not in visible_paths:
                    continue
                parent = PurePosixPath(result.path).parent
                if str(parent) == "." or not (self.vault / str(parent)).is_dir():
                    continue
                evidence[result.note_id] += 1.0
        directory_scores: dict[str, float] = defaultdict(float)
        candidate_notes = {}
        for note_id, score in evidence.items():
            record = self.repository.notes.get(note_id)
            if record is None:
                continue
            directory = str(PurePosixPath(record.path).parent)
            if directory == self.inbox or directory.startswith(self.inbox + "/"):
                continue
            directory_scores[directory] += score
            candidate_notes[note_id] = (record, score)
        ranked = sorted(directory_scores.items(), key=lambda item: (-item[1], item[0]))
        if not ranked or (len(ranked) > 1 and ranked[0][1] <= ranked[1][1] * 1.25):
            reason = "No related indexed notes in a clear existing directory" if not ranked else "Related notes point to multiple directories"
            return OrganizerProposal(note.path, None, note.title, (), (), (), 0.0, reason)
        directory, score = ranked[0]
        destination = f"{directory}/{_filename(note)}"
        related = sorted(
            ((record, value) for record, value in candidate_notes.values()
             if str(PurePosixPath(record.path).parent) == directory),
            key=lambda item: (-item[1], item[0].path),
        )
        existing_targets = {link.target_path for link in note.wikilinks}
        links = tuple(
            RelatedLink(record.path, record.title) for record, _ in related[:2]
            if record.path[:-3] not in existing_targets and record.path not in existing_targets
            and not any(char in record.path for char in "[]|\r\n")
        )
        tags = []
        for record, _ in related[:2]:
            for tag in self.repository.tags_for_note(record.id):
                if tag not in note.tags and tag not in tags:
                    tags.append(tag)
        issue = None
        impacts: tuple[str, ...] = ()
        try:
            impacts = self.safe.move_note(note.path, destination).file.affected_backlinks
        except CollisionError:
            issue = "Destination already exists"
        # A lone keyword match is reviewable but deliberately not auto-selected.
        confidence = min(0.95, 0.55 + 0.10 * score)
        reason = f"Matched {len(related)} indexed note(s) in existing directory {directory}"
        return OrganizerProposal(note.path, destination, note.title, tuple(tags[:5]), links,
                                 impacts, confidence, reason, issue)

    def plan(self, proposals: Sequence[OrganizerProposal], selected: Sequence[int]) -> TransactionPlan:
        """Indices are one-based and every chosen proposal belongs to one logical transaction."""
        if not selected or len(set(selected)) != len(selected):
            raise TransactionError("Select at least one distinct proposal")
        chosen = []
        for index in selected:
            check_shutdown()
            if index < 1 or index > len(proposals):
                raise TransactionError(f"Invalid proposal number: {index}")
            proposal = proposals[index - 1]
            if not proposal.actionable:
                raise TransactionError(f"Proposal {index} has no safe destination: {proposal.issue or proposal.reason}")
            chosen.append(proposal)
        moves = [(proposal.path, proposal.destination) for proposal in chosen]
        extras: list[TransactionOperation] = []
        for proposal in chosen:
            check_shutdown()
            assert proposal.destination is not None
            parsed = parse_note(self.vault / proposal.path, vault_root=self.vault)
            if proposal.add_tags:
                current = parsed.frontmatter.get("tags", [])
                if not isinstance(current, (str, list)):
                    raise TransactionError(f"Unsupported existing tags format: {proposal.path}")
                current_tags = [current] if isinstance(current, str) else list(current)
                if any(not isinstance(tag, str) for tag in current_tags):
                    raise TransactionError(f"Unsupported existing tags format: {proposal.path}")
                extras.append(TransactionOperation.frontmatter(
                    proposal.destination,
                    {"tags": list(dict.fromkeys([*current_tags, *proposal.add_tags]))},
                ))
            if proposal.add_links:
                suffix = "\n\nRelated:\n" + "".join(f"- {link.wikilink}\n" for link in proposal.add_links)
                extras.append(TransactionOperation.append(proposal.destination, suffix))
        return self.transaction.plan_moves_with_backlinks(moves, extras)

    def apply(self, plan: TransactionPlan) -> TransactionResult:
        return self.transaction.execute(plan, approved=True)
