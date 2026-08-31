from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field


_FILES_MARKER_RE = re.compile(r"(?m)^[ \t]*\|[ \t]*\*{5}FILES\*{5}[ \t]*\r?$")
_WORK_INFO_MARKER_RE = re.compile(r"(?m)^[ \t]*\|[ \t]*\*{5}WORK INFO\*{5}[ \t]*\r?$")
_HEADING_RE = re.compile(r"(?m)^(?P<marks>={2,6})[ \t]*(?P<text>.*?)[ \t]*(?P=marks)[ \t]*$")
_FILE_TEMPLATE_RE = re.compile(r"\{\{#fte:imslpfile\b.*?\}\}", re.IGNORECASE | re.DOTALL)
_FILE_NAME_RE = re.compile(r"(?mi)^\|File Name 1[ \t]*=[ \t]*(?P<value>[^\r\n]*)$")
_FILE_ID_RE = re.compile(r"(?mi)^\|File ID[ \t]*=[ \t]*(?P<value>[^\r\n]*)$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TEMPLATE_RE = re.compile(r"\{\{([^{}]*)\}\}")
_WIKILINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")
_EXTERNAL_LINK_RE = re.compile(r"\[(?:https?://\S+)(?:\s+([^\]]+))?\]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MEDIAWIKI_QUOTES_RE = re.compile(r"'{2,5}")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_heading(text: str) -> str:
    """Normalize a heading without erasing punctuation or relationship words."""

    if not isinstance(text, str):
        raise TypeError("heading text must be a string")
    value = unicodedata.normalize("NFC", html.unescape(text))
    value = _COMMENT_RE.sub("", value)
    while _TEMPLATE_RE.search(value) is not None:
        value = _TEMPLATE_RE.sub(
            lambda match: match.group(1).rsplit("|", 1)[-1] if "|" in match.group(1) else "",
            value,
        )
    value = _WIKILINK_RE.sub(lambda match: match.group(1), value)
    value = _EXTERNAL_LINK_RE.sub(lambda match: match.group(1) or "", value)
    value = _HTML_TAG_RE.sub(" ", value)
    value = _MEDIAWIKI_QUOTES_RE.sub("", value)
    value = unicodedata.normalize("NFC", value)
    folded = _WHITESPACE_RE.sub(" ", value).strip().casefold()
    return unicodedata.normalize("NFC", folded)


@dataclass(frozen=True, slots=True)
class FileTemplateChunk:
    raw: str
    file_id: str
    filename: str
    source_span: tuple[int, int]


@dataclass(slots=True)
class HeadingNode:
    raw: str
    normalized: str
    level: int
    parent: HeadingNode | None
    source_span: tuple[int, int]
    children: list[HeadingNode] = field(default_factory=list)
    file_templates: list[FileTemplateChunk] = field(default_factory=list)

    @property
    def ancestry(self) -> tuple[str, ...]:
        values: list[str] = []
        node: HeadingNode | None = self
        while node is not None:
            values.append(node.raw)
            node = node.parent
        return tuple(reversed(values))

    @property
    def normalized_ancestry(self) -> tuple[str, ...]:
        values: list[str] = []
        node: HeadingNode | None = self
        while node is not None:
            values.append(node.normalized)
            node = node.parent
        return tuple(reversed(values))


@dataclass(slots=True)
class HeadingTree:
    roots: list[HeadingNode]
    files_region_span: tuple[int, int]
    unheaded_file_templates: list[FileTemplateChunk] = field(default_factory=list)

    @property
    def file_templates(self) -> tuple[FileTemplateChunk, ...]:
        chunks: list[FileTemplateChunk] = list(self.unheaded_file_templates)

        def visit(node: HeadingNode) -> None:
            chunks.extend(node.file_templates)
            for child in node.children:
                visit(child)

        for root in self.roots:
            visit(root)
        return tuple(sorted(chunks, key=lambda chunk: chunk.source_span))

    @property
    def nodes(self) -> tuple[HeadingNode, ...]:
        values: list[HeadingNode] = []

        def visit(node: HeadingNode) -> None:
            values.append(node)
            for child in node.children:
                visit(child)

        for root in self.roots:
            visit(root)
        return tuple(values)


def _files_region(wikitext: str) -> tuple[str, int, int]:
    marker_text = _COMMENT_RE.sub(
        lambda match: "".join(character if character in "\r\n" else " " for character in match.group(0)),
        wikitext,
    )
    files_match = _FILES_MARKER_RE.search(marker_text)
    if files_match is None:
        return "", 0, 0
    work_info_match = _WORK_INFO_MARKER_RE.search(marker_text, files_match.end())
    if work_info_match is None:
        return "", 0, 0
    start = files_match.end()
    end = work_info_match.start()
    return wikitext[start:end], start, end


def _file_chunk(match: re.Match[str], region_offset: int) -> FileTemplateChunk | None:
    raw = match.group(0)
    filename_match = _FILE_NAME_RE.search(raw)
    file_id_match = _FILE_ID_RE.search(raw)
    if filename_match is None or file_id_match is None:
        return None
    filename = html.unescape(filename_match.group("value")).strip()
    file_id = file_id_match.group("value").strip()
    if not filename or not file_id:
        return None
    return FileTemplateChunk(
        raw=raw,
        file_id=file_id,
        filename=filename,
        source_span=(region_offset + match.start(), region_offset + match.end()),
    )


def parse_heading_tree(wikitext: str) -> HeadingTree:
    """Parse headings and file-template chunks from only the IMSLP FILES region."""

    if not isinstance(wikitext, str):
        raise TypeError("wikitext must be a string")
    region, region_start, region_end = _files_region(wikitext)
    tree = HeadingTree(roots=[], files_region_span=(region_start, region_end))
    comment_spans = tuple(match.span() for match in _COMMENT_RE.finditer(region))

    def outside_comment(match: re.Match[str]) -> bool:
        return not any(start <= match.start() < end for start, end in comment_spans)

    events: list[tuple[int, int, re.Match[str]]] = []
    events.extend(
        (match.start(), 0, match)
        for match in _HEADING_RE.finditer(region)
        if outside_comment(match)
    )
    events.extend(
        (match.start(), 1, match)
        for match in _FILE_TEMPLATE_RE.finditer(region)
        if outside_comment(match)
    )
    events.sort(key=lambda item: (item[0], item[1]))

    stack: list[HeadingNode] = []
    current: HeadingNode | None = None
    for _, event_kind, match in events:
        if event_kind == 0:
            level = len(match.group("marks"))
            raw = match.group("text").strip()
            while stack and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1] if stack else None
            node = HeadingNode(
                raw=raw,
                normalized=normalize_heading(raw),
                level=level,
                parent=parent,
                source_span=(region_start + match.start(), region_start + match.end()),
            )
            if parent is None:
                tree.roots.append(node)
            else:
                parent.children.append(node)
            stack.append(node)
            current = node
            continue

        chunk = _file_chunk(match, region_start)
        if chunk is None:
            continue
        if current is None:
            tree.unheaded_file_templates.append(chunk)
        else:
            current.file_templates.append(chunk)
    return tree
