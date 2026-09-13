"""Shared SQL metadata filter semantics for FTS and vector retrieval."""

from datetime import datetime, timezone

from obsai.retrieval.models import JsonScalar, SearchFilters


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _json_scalar(value: JsonScalar) -> tuple[str, object]:
    if value is None:
        return "null", None
    if isinstance(value, bool):
        return ("true" if value else "false"), int(value)
    if isinstance(value, int):
        return "integer", value
    if isinstance(value, float):
        return "real", value
    return "text", value


def metadata_conditions(
    filters: SearchFilters | None, *, notes_alias: str = "notes"
) -> tuple[list[str], list[object]]:
    """Return parameterized WHERE clauses; alias is internal, never user input."""
    if filters is None:
        return [], []
    clauses: list[str] = []
    params: list[object] = []
    for tag in filters.tags:
        clauses.append(
            f"EXISTS (SELECT 1 FROM tags WHERE tags.note_id = {notes_alias}.id AND tags.tag = ?)"
        )
        params.append(tag.lstrip("#"))
    if filters.folder:
        folder = filters.folder.strip("/")
        if folder:
            prefix = folder + "/"
            clauses.append(f"substr({notes_alias}.path, 1, length(?)) = ?")
            params.extend((prefix, prefix))
    if filters.modified_after:
        clauses.append(f"{notes_alias}.modified_at >= ?")
        params.append(_timestamp(filters.modified_after))
    if filters.modified_before:
        clauses.append(f"{notes_alias}.modified_at <= ?")
        params.append(_timestamp(filters.modified_before))
    for column, fields in (
        ("frontmatter_json", filters.frontmatter),
        ("dataview_json", filters.dataview),
    ):
        for key, value in fields.items():
            kind, sql_value = _json_scalar(value)
            clauses.append(
                f"EXISTS (SELECT 1 FROM json_each({notes_alias}.{column}) AS field "
                "WHERE field.key = ? AND field.type = ? AND field.value IS ?)"
            )
            params.extend((key, kind, sql_value))
    return clauses, params
