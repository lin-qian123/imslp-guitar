from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field


_FILES_MARKER_RE = re.compile(r"(?m)^[ \t]*\|[ \t]*\*{5}FILES\*{5}[ \t]*(?:=[ \t]*)?\r?$")
_WORK_INFO_MARKER_RE = re.compile(r"(?m)^[ \t]*\|[ \t]*\*{5}WORK INFO\*{5}[ \t]*(?:=[ \t]*)?\r?$")
_HEADING_RE = re.compile(r"(?m)^(?P<marks>={2,6})[ \t]*(?P<text>.*?)[ \t]*(?P=marks)[ \t]*$")
_FTE_START_RE = re.compile(r"^\{\{[ \t]*#fte:imslpfile\b", re.IGNORECASE)
_FILE_NAME_RE = re.compile(
    r"(?mi)^[ \t]*\|{1,2}[ \t]*File Name[ \t]+(?P<index>[1-9][0-9]*)"
    r"[ \t]*=[ \t]*(?P<value>[^\r\n]*)$"
)
_FILE_ID_RE = re.compile(
    r"(?mi)^[ \t]*\|{1,2}[ \t]*File ID(?:[ \t]+(?P<index>[1-9][0-9]*))?"
    r"[ \t]*=[ \t]*(?P<value>[^\r\n]*)$"
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TEMPLATE_RE = re.compile(r"\{\{([^{}]*)\}\}")
_WIKILINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")
_EXTERNAL_LINK_RE = re.compile(r"\[(?:https?://\S+)(?:\s+([^\]]+))?\]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MEDIAWIKI_QUOTES_RE = re.compile(r"'{2,5}")
_WHITESPACE_RE = re.compile(r"\s+")


class HeadingParseError(ValueError):
    """A stable fail-closed wikitext structure error."""


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
    file_id: str | None
    filename: str
    attachment_index: int
    file_id_conflict: bool
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


def _balanced_templates(searchable_region: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    index = 0
    while index < len(searchable_region) - 1:
        token = searchable_region[index:index + 2]
        if token == "{{":
            if depth == 0:
                start = index
            depth += 1
            index += 2
            continue
        if token == "}}" and depth:
            depth -= 1
            index += 2
            if depth == 0:
                spans.append((start, index))
                start = -1
            continue
        index += 1
    if depth:
        raise HeadingParseError("unbalanced_template")
    return tuple(spans)


def _file_chunks(raw: str, start: int, end: int) -> tuple[FileTemplateChunk, ...]:
    filenames = tuple(_FILE_NAME_RE.finditer(raw))
    if not filenames:
        raise HeadingParseError("file_template_missing_filename")
    indexed_ids: dict[int, list[str]] = {}
    unindexed_ids: list[str] = []
    for match in _FILE_ID_RE.finditer(raw):
        value = match.group("value").strip()
        if match.group("index") is None:
            unindexed_ids.append(value)
        else:
            indexed_ids.setdefault(int(match.group("index")), []).append(value)

    chunks: list[tuple[int, int, FileTemplateChunk]] = []
    for filename_match in filenames:
        attachment_index = int(filename_match.group("index"))
        filename = html.unescape(filename_match.group("value")).strip()
        if not filename:
            raise HeadingParseError("file_template_empty_filename")
        id_values = unindexed_ids + indexed_ids.get(attachment_index, [])
        unique_ids = tuple(dict.fromkeys(id_values))
        chunks.append((
            attachment_index,
            filename_match.start(),
            FileTemplateChunk(
                raw=raw,
                file_id=unique_ids[0] if len(unique_ids) == 1 else None,
                filename=filename,
                attachment_index=attachment_index,
                file_id_conflict=len(unique_ids) > 1,
                source_span=(start, end),
            ),
        ))
    chunks.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in chunks)


def parse_heading_tree(wikitext: str) -> HeadingTree:
    """Parse headings and file-template chunks from only the IMSLP FILES region."""

    if not isinstance(wikitext, str):
        raise TypeError("wikitext must be a string")
    region, region_start, region_end = _files_region(wikitext)
    tree = HeadingTree(roots=[], files_region_span=(region_start, region_end))
    comment_spans = tuple(match.span() for match in _COMMENT_RE.finditer(region))
    searchable_region = _COMMENT_RE.sub(
        lambda match: "".join(character if character in "\r\n" else " " for character in match.group(0)),
        region,
    )

    def outside_comment(match: re.Match[str]) -> bool:
        return not any(start <= match.start() < end for start, end in comment_spans)

    events: list[tuple[int, int, object]] = []
    events.extend(
        (match.start(), 0, match)
        for match in _HEADING_RE.finditer(region)
        if outside_comment(match)
    )
    events.extend(
        (start, 1, (start, end))
        for start, end in _balanced_templates(searchable_region)
        if _FTE_START_RE.match(searchable_region[start:end]) is not None
    )
    events.sort(key=lambda item: (item[0], item[1]))

    stack: list[HeadingNode] = []
    current: HeadingNode | None = None
    for _, event_kind, payload in events:
        if event_kind == 0:
            if not isinstance(payload, re.Match):
                raise AssertionError("heading event must contain a regex match")
            match = payload
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

        if not isinstance(payload, tuple) or len(payload) != 2:
            raise AssertionError("file-template event must contain a source span")
        template_start, template_end = payload
        raw = region[template_start:template_end]
        chunks = _file_chunks(
            raw,
            region_start + template_start,
            region_start + template_end,
        )
        if current is None:
            tree.unheaded_file_templates.extend(chunks)
        else:
            current.file_templates.extend(chunks)
    return tree
