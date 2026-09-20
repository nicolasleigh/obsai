"""One note, resolved out of the derived index.

``/search`` and ``/ask`` both end at a note: a result card and a citation card are
the two ways a user arrives here, and until this route existed both were dead ends.

Three decisions are worth stating.

* **The path parameter is the note ID, not the note path.** The ID is what the
  search results and citations already carry, it survives a rename (the indexer
  keeps it), it needs no encoding in a URL segment, and it is unambiguous on a
  case-insensitive filesystem in a way a path is not.
* **No ``get_settings`` dependency.** The note comes out of the index, so this
  route needs neither ``vault.path`` nor any embedding configuration — the same
  reason ``/search`` does not. A Vault that has moved since the last index update
  is still readable here, which matters because a user reaching this page has just
  clicked something that was working.
* **Nothing about the response is HTML.** The note arrives as text runs and
  resolved links; see :mod:`obsai.application.notes` for why the source Markdown
  never crosses the wire. The renderer's whole job is text nodes and ``<Link>``s,
  so "no script executes" is a property of the shape rather than of a sanitiser.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from obsai.api.deps import index_handle
from obsai.application.dto import NoteView
from obsai.application.index import IndexHandle
from obsai.application.notes import read_note

router = APIRouter(tags=["notes"])


@router.get("/notes/{note_id}", response_model=NoteView)
def get_note(note_id: str, index: IndexHandle = Depends(index_handle)) -> NoteView:
    """Render one note, or 404 when the ID is not in the index.

    An unknown ID is a 404 rather than a 400: the request was well-formed and the
    configuration is fine, the note is simply gone. A bookmark into a note that was
    deleted and re-indexed is the ordinary way to get here, and the UI can say
    "this note no longer exists" instead of asking the user to check their config.
    """
    # A missing index stays the 400 it is everywhere else — the same message the CLI
    # prints. Without an index there are no note IDs at all, so "not built yet" is
    # the only useful thing to say, and it is not the same problem as "no such note".
    return read_note(note_id, database=index.require())
