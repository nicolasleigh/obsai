"""Conservative, syntax-aware rewrite of explicit vault-root WikiLink paths."""

from collections import Counter
from pathlib import PurePosixPath

from obsai.vault.parser import WIKILINK_RE, parse_markdown


def _replacement(inner: str, old: str, new: str) -> str | None:
    destination, alias_separator, alias = inner.partition("|")
    target, fragment_separator, fragment = destination.partition("#")
    if target != target.strip():
        return None
    explicit = "/" in target or target.lower().endswith(".md")
    normalized = target[:-3] if target.lower().endswith(".md") else target
    if not explicit or normalized != old[:-3]:
        return None
    new_target = new if target.lower().endswith(".md") else new[:-3]
    return new_target + (fragment_separator + fragment if fragment_separator else "") + (
        alias_separator + alias if alias_separator else ""
    )


def rewrite_explicit_links(
    content: str, source_path: str, old_path: str, new_path: str,
) -> tuple[str, int, int]:
    """Only edit links confirmed by the parser; skip ambiguous and mixed syntax lines."""
    parsed = parse_markdown(content, source_path)
    explicit_by_line: Counter[int] = Counter()
    ambiguous = 0
    old_stem = PurePosixPath(old_path).stem
    for link in parsed.wikilinks:
        target = link.target_path
        if not target:
            continue
        if _replacement(target, old_path, new_path) is not None:
            explicit_by_line[link.line] += 1
        elif (target[:-3] if target.lower().endswith(".md") else target) == old_stem:
            ambiguous += 1

    rewritten = 0
    lines = content.splitlines(keepends=True)
    for number, line in enumerate(lines, start=1):
        candidates = [
            match for match in WIKILINK_RE.finditer(line)
            if _replacement(match.group("target"), old_path, new_path) is not None
        ]
        if not candidates or len(candidates) != explicit_by_line[number]:
            continue

        def replace_match(match):
            replacement = _replacement(match.group("target"), old_path, new_path)
            if replacement is None:
                return match.group(0)
            return match.group(0)[:match.start("target") - match.start()] + replacement + "]]"

        updated = WIKILINK_RE.sub(replace_match, line)
        if updated != line:
            lines[number - 1] = updated
            rewritten += len(candidates)
    return "".join(lines), rewritten, ambiguous
