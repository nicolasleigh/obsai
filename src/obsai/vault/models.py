"""Stable, serializable parser output; paths are relative to the vault."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ParserModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Heading(ParserModel):
    level: int
    text: str
    line: int


class Block(ParserModel):
    kind: Literal["heading", "paragraph", "list_item", "blockquote", "callout", "code"]
    content: str
    line: int
    end_line: int
    raw_content: str
    block_id: str | None = None
    language: str | None = None


class WikiLink(ParserModel):
    target_path: str | None
    target_heading: str | None = None
    target_block_id: str | None = None
    display_text: str | None = None
    is_embed: bool = False
    line: int


class ExternalLink(ParserModel):
    url: str
    display_text: str
    is_embed: bool = False
    line: int


class Callout(ParserModel):
    callout_type: str
    content: str
    line: int
    end_line: int
    raw_content: str


class BlockReference(ParserModel):
    block_id: str
    content: str
    line: int


class ParsedNote(ParserModel):
    path: str
    title: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    dataview_fields: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    headings: list[Heading] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    wikilinks: list[WikiLink] = Field(default_factory=list)
    external_links: list[ExternalLink] = Field(default_factory=list)
    callouts: list[Callout] = Field(default_factory=list)
    block_references: list[BlockReference] = Field(default_factory=list)
    raw_content: str
    plain_text: str
