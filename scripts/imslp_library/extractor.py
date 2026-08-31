from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import replace
from pathlib import Path

from .config import CategoryConfig
from .enums import CategoryKind, SelectionReason
from .headings import FileTemplateChunk, HeadingNode, HeadingTree, normalize_heading, parse_heading_tree
from .models import ExtractionDecision, ExtractionResult, ExtractionReview, FrozenPage, SelectionEvidence


_INSTRUMENTATION_RE = re.compile(r"(?m)^\|Instrumentation=(?P<value>[^\r\n]*)$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_RELATIONSHIP_SUFFIX_RE = re.compile(r"^(?:and|or|with|plus)\b|^[,&/+]")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_ARRANGEMENT_BRANCH = "arrangements and transcriptions"
_ORIGINAL_BRANCH = "scores and parts"


def _instrumentation(wikitext: str, work_info_offset: int) -> tuple[str | None, str | None]:
    region = wikitext[work_info_offset:]
    searchable = _COMMENT_RE.sub(
        lambda match: "".join(character if character in "\r\n" else " " for character in match.group(0)),
        region,
    )
    match = _INSTRUMENTATION_RE.search(searchable)
    if match is None:
        return None, None
    raw = region[match.start("value"):match.end("value")].strip()
    if not raw:
        return None, None
    return raw, normalize_heading(raw)


def _branch(node: HeadingNode) -> HeadingNode:
    while node.parent is not None:
        node = node.parent
    return node


def _node_chunks(tree: HeadingTree) -> tuple[tuple[HeadingNode | None, FileTemplateChunk], ...]:
    pairs: list[tuple[HeadingNode | None, FileTemplateChunk]] = [(None, chunk) for chunk in tree.unheaded_file_templates]

    def visit(node: HeadingNode) -> None:
        pairs.extend((node, chunk) for chunk in node.file_templates)
        for child in node.children:
            visit(child)

    for root in tree.roots:
        visit(root)
    return tuple(sorted(pairs, key=lambda pair: pair[1].source_span))


def _evidence(
    node: HeadingNode | None,
    instrumentation_raw: str | None,
    instrumentation_normalized: str | None,
    detail: str,
) -> SelectionEvidence:
    branch = _branch(node).raw if node is not None else "FILES"
    return SelectionEvidence(
        heading_raw=node.raw if node is not None else None,
        heading_normalized=node.normalized if node is not None else None,
        heading_ancestry=node.ancestry if node is not None else (),
        heading_ancestry_normalized=node.normalized_ancestry if node is not None else (),
        instrumentation_raw=instrumentation_raw,
        instrumentation_normalized=instrumentation_normalized,
        branch=branch,
        reason_detail=detail,
    )


def _decision(
    page: FrozenPage,
    chunk: FileTemplateChunk,
    node: HeadingNode | None,
    instrumentation_raw: str | None,
    instrumentation_normalized: str | None,
    *,
    disposition: str,
    reason_code: str,
    detail: str,
    selection_reason: SelectionReason | None = None,
) -> ExtractionDecision:
    return ExtractionDecision(
        source_id=f"source:f{chunk.file_id}@r{page.revision_id}",
        filename=chunk.filename,
        disposition=disposition,
        reason_code=reason_code,
        selection_reason=selection_reason,
        evidence=_evidence(node, instrumentation_raw, instrumentation_normalized, detail),
    )


def _annotation_status(category: CategoryConfig, normalized: str) -> str:
    for pattern in category.heading_patterns:
        match = pattern.fullmatch(normalized)
        if match is None:
            continue
        annotation = match.groupdict().get("annotation")
        if annotation is None:
            return "exact"
        tokens = {token.casefold() for token in _TOKEN_RE.findall(annotation)}
        return "annotation" if tokens & category.annotation_reject_tokens else "exact"
    return "none"


def _target_base(category: CategoryConfig) -> str:
    return normalize_heading(category.name.removesuffix(" (arr)"))


def _is_mixed_target_heading(category: CategoryConfig, normalized: str) -> bool:
    base = _target_base(category)
    if not normalized.startswith(base):
        return False
    suffix = normalized[len(base):].lstrip()
    if not suffix or suffix.startswith("("):
        return False
    return _RELATIONSHIP_SUFFIX_RE.match(suffix) is not None


def _is_mixed_descendant(category: CategoryConfig, node: HeadingNode) -> bool:
    tokens = {token.casefold() for token in _TOKEN_RE.findall(node.normalized)}
    other_instruments = category.annotation_reject_tokens - {"guitar"}
    return bool(tokens & other_instruments)


def _mixed_ancestry_node(category: CategoryConfig, node: HeadingNode) -> HeadingNode | None:
    ancestry: list[HeadingNode] = []
    cursor: HeadingNode | None = node
    while cursor is not None:
        ancestry.append(cursor)
        cursor = cursor.parent
    ancestry.reverse()
    for candidate in ancestry[1:]:
        if _annotation_status(category, candidate.normalized) in {"exact", "annotation"}:
            continue
        if _is_mixed_target_heading(category, candidate.normalized):
            continue
        if _is_mixed_descendant(category, candidate):
            return candidate
    return None


def _target_context(category: CategoryConfig, node: HeadingNode) -> tuple[str, HeadingNode | None]:
    ancestry: list[HeadingNode] = []
    cursor: HeadingNode | None = node
    while cursor is not None:
        ancestry.append(cursor)
        cursor = cursor.parent
    ancestry.reverse()

    target: HeadingNode | None = None
    for candidate in ancestry[1:]:
        status = _annotation_status(category, candidate.normalized)
        if status == "annotation":
            return "annotation", candidate
        if _is_mixed_target_heading(category, candidate.normalized):
            return "mixed", candidate
        if status == "exact":
            target = candidate
            continue
        if target is not None and _is_mixed_descendant(category, candidate):
            return "mixed", candidate
    return ("exact", target) if target is not None else ("none", None)


def _has_arrangement_branch(tree: HeadingTree) -> bool:
    return any(root.normalized == _ARRANGEMENT_BRANCH for root in tree.roots)


def _has_exact_target_heading(tree: HeadingTree, category: CategoryConfig) -> bool:
    for root in tree.roots:
        if root.normalized != _ARRANGEMENT_BRANCH:
            continue
        for node in (root, *tuple(_descendants(root))):
            if _annotation_status(category, node.normalized) == "exact":
                return True
    return False


def _descendants(node: HeadingNode):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _extract_original(
    page: FrozenPage,
    chunk: FileTemplateChunk,
    node: HeadingNode,
    category: CategoryConfig,
    instrumentation_raw: str | None,
    instrumentation_normalized: str | None,
) -> ExtractionDecision:
    if instrumentation_normalized is None:
        return _decision(
            page, chunk, node, instrumentation_raw, instrumentation_normalized,
            disposition="excluded", reason_code="instrumentation_missing", detail="original instrumentation is missing",
        )
    if not category.matches_instrumentation(instrumentation_normalized):
        return _decision(
            page, chunk, node, instrumentation_raw, instrumentation_normalized,
            disposition="excluded", reason_code="instrumentation_not_exact", detail="original instrumentation did not fullmatch",
        )
    return _decision(
        page, chunk, node, instrumentation_raw, instrumentation_normalized,
        disposition="selected", reason_code="exact_original_instrumentation",
        selection_reason=SelectionReason.EXACT_ORIGINAL_INSTRUMENTATION,
        detail="category membership, original branch and exact instrumentation",
    )


def _extract_arrangement_heading(
    page: FrozenPage,
    chunk: FileTemplateChunk,
    node: HeadingNode,
    category: CategoryConfig,
    instrumentation_raw: str | None,
    instrumentation_normalized: str | None,
) -> ExtractionDecision:
    status, evidence_node = _target_context(category, node)
    if status == "annotation":
        return _decision(
            page, chunk, evidence_node or node, instrumentation_raw, instrumentation_normalized,
            disposition="excluded", reason_code="annotation_contains_instrument",
            detail="target heading annotation contains a rejected instrument token",
        )
    if status == "mixed":
        return _decision(
            page, chunk, evidence_node or node, instrumentation_raw, instrumentation_normalized,
            disposition="excluded", reason_code="mixed_instrument_heading",
            detail="target prefix or descendant includes mixed instrumentation",
        )
    if status != "exact":
        return _decision(
            page, chunk, node, instrumentation_raw, instrumentation_normalized,
            disposition="excluded", reason_code="heading_not_exact",
            detail="arrangement heading did not fullmatch the configured target",
        )
    return _decision(
        page, chunk, node, instrumentation_raw, instrumentation_normalized,
        disposition="selected", reason_code="exact_arrangement_heading",
        selection_reason=SelectionReason.EXACT_ARRANGEMENT_HEADING,
        detail="category membership, arrangement branch and exact target heading",
    )


def _extract_work_level(
    page: FrozenPage,
    chunk: FileTemplateChunk,
    node: HeadingNode,
    category: CategoryConfig,
    tree: HeadingTree,
    instrumentation_raw: str | None,
    instrumentation_normalized: str | None,
) -> ExtractionDecision:
    if _has_exact_target_heading(tree, category):
        return _decision(
            page, chunk, node, instrumentation_raw, instrumentation_normalized,
            disposition="excluded", reason_code="work_level_target_heading_present",
            detail="an exact target arrangement heading is present on this page",
        )
    if _has_arrangement_branch(tree):
        return _decision(
            page, chunk, node, instrumentation_raw, instrumentation_normalized,
            disposition="excluded", reason_code="work_level_arrangement_branch_present",
            detail="an arrangement branch is present, so the original score is not a work-level exception",
        )
    if instrumentation_normalized is None:
        return _decision(
            page, chunk, node, instrumentation_raw, instrumentation_normalized,
            disposition="manual_review", reason_code="work_level_instrumentation_missing",
            detail="work-level exception has no instrumentation evidence",
        )
    if not category.matches_instrumentation(instrumentation_normalized):
        return _decision(
            page, chunk, node, instrumentation_raw, instrumentation_normalized,
            disposition="manual_review", reason_code="work_level_instrumentation_not_exact",
            detail="work-level instrumentation did not fullmatch the configured target",
        )
    return _decision(
        page, chunk, node, instrumentation_raw, instrumentation_normalized,
        disposition="selected", reason_code="work_level_exact_instrumentation",
        selection_reason=SelectionReason.WORK_LEVEL_EXACT_INSTRUMENTATION,
        detail="category membership, work-level branch, exact instrumentation and no arrangement branch",
    )


def extract_memberships(
    page: FrozenPage,
    wikitext: str,
    category: CategoryConfig,
) -> ExtractionResult:
    """Classify all IMSLP file templates for one frozen page/category revision."""

    if not isinstance(page, FrozenPage):
        raise TypeError("page must be a FrozenPage")
    if not isinstance(wikitext, str):
        raise TypeError("wikitext must be a string")
    if not isinstance(category, CategoryConfig):
        raise TypeError("category must be a CategoryConfig")
    actual_digest = hashlib.sha256(wikitext.encode("utf-8")).hexdigest()
    if actual_digest != page.wikitext_sha256:
        raise ValueError("wikitext_sha256_mismatch")

    tree = parse_heading_tree(wikitext)
    instrumentation_raw, instrumentation_normalized = _instrumentation(
        wikitext, tree.files_region_span[1]
    )
    decisions: list[ExtractionDecision] = []
    seen_file_ids: set[str] = set()
    for node, chunk in _node_chunks(tree):
        if chunk.file_id in seen_file_ids:
            continue
        seen_file_ids.add(chunk.file_id)
        if category.name not in page.category_names:
            decisions.append(_decision(
                page, chunk, node, instrumentation_raw, instrumentation_normalized,
                disposition="excluded", reason_code="page_not_in_category",
                detail="frozen page membership does not contain the target category",
            ))
            continue
        if node is None:
            decisions.append(_decision(
                page, chunk, node, instrumentation_raw, instrumentation_normalized,
                disposition="excluded", reason_code="branch_not_allowed",
                detail="file template has no heading branch",
            ))
            continue
        branch = _branch(node).normalized
        allowed_branch = branch == _ORIGINAL_BRANCH if category.kind is CategoryKind.ORIGINAL else branch in {_ORIGINAL_BRANCH, _ARRANGEMENT_BRANCH}
        if not allowed_branch:
            decisions.append(_decision(
                page, chunk, node, instrumentation_raw, instrumentation_normalized,
                disposition="excluded", reason_code="branch_not_allowed",
                detail="file template is outside an allowed score branch",
            ))
            continue
        if not chunk.filename.casefold().endswith(".pdf"):
            decisions.append(_decision(
                page, chunk, node, instrumentation_raw, instrumentation_normalized,
                disposition="excluded", reason_code="not_pdf",
                detail="file attachment does not have a PDF filename",
            ))
            continue

        mixed_ancestry_node = _mixed_ancestry_node(category, node)
        if mixed_ancestry_node is not None:
            decisions.append(_decision(
                page, chunk, mixed_ancestry_node,
                instrumentation_raw, instrumentation_normalized,
                disposition="excluded", reason_code="mixed_instrument_heading",
                detail="heading ancestry contains mixed instrumentation",
            ))
            continue

        if category.kind is CategoryKind.ORIGINAL:
            decisions.append(_extract_original(
                page, chunk, node, category, instrumentation_raw, instrumentation_normalized,
            ))
        elif branch == _ARRANGEMENT_BRANCH:
            decisions.append(_extract_arrangement_heading(
                page, chunk, node, category, instrumentation_raw, instrumentation_normalized,
            ))
        else:
            decisions.append(_extract_work_level(
                page, chunk, node, category, tree, instrumentation_raw, instrumentation_normalized,
            ))
    return ExtractionResult(
        page_id=page.page_id,
        revision_id=page.revision_id,
        category_name=category.name,
        decisions=tuple(decisions),
    )


def extraction_decision_sha256(decision: ExtractionDecision) -> str:
    if not isinstance(decision, ExtractionDecision):
        raise TypeError("decision must be an ExtractionDecision")
    canonical = json.dumps(
        decision.to_dict(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def review_filename(category_name: str, page_id: int, source_id: str | None) -> str:
    if not isinstance(category_name, str) or not category_name.strip():
        raise ValueError("category_name must be nonblank")
    if type(page_id) is not int or page_id < 0:
        raise ValueError("page_id must be a nonnegative integer")
    category_nfc = unicodedata.normalize("NFC", category_name)
    category_sha10 = hashlib.sha256(category_nfc.encode("utf-8")).hexdigest()[:10]
    source_component = source_id if source_id is not None else "page"
    return f"{category_sha10}-p{page_id}-{source_component}.json"


def _validate_review_directory(review_directory: Path, run_id: str) -> None:
    if (
        review_directory.name != run_id
        or review_directory.parent.name != "extraction_reviews"
        or review_directory.parent.parent.name != "overrides"
        or review_directory.parent.parent.parent.name != "metadata"
    ):
        raise ValueError("review directory must be metadata/overrides/extraction_reviews/<run-id>")


def apply_extraction_reviews(
    result: ExtractionResult,
    *,
    run_id: str,
    review_directory: Path,
) -> ExtractionResult:
    """Replay exact exclusion-only reviews against manual-review decisions."""

    if not isinstance(result, ExtractionResult):
        raise TypeError("result must be an ExtractionResult")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be nonblank")
    review_directory = Path(review_directory)
    _validate_review_directory(review_directory, run_id)
    if not review_directory.exists():
        return result
    if not review_directory.is_dir():
        raise ValueError("review directory is not a directory")

    manual_by_name = {
        review_filename(result.category_name, result.page_id, decision.source_id): decision
        for decision in result.manual_review
    }
    actual_files = {path.name: path for path in review_directory.iterdir() if path.is_file()}
    unexpected = sorted(set(actual_files) - set(manual_by_name))
    if unexpected:
        raise ValueError(f"unexpected extraction review: {unexpected[0]}")

    replacements: dict[int, ExtractionDecision] = {}
    for filename, decision in manual_by_name.items():
        path = actual_files.get(filename)
        if path is None:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise
        review = ExtractionReview.from_dict(payload)
        expected_digest = extraction_decision_sha256(decision)
        if review.run_id != run_id:
            raise ValueError("extraction review run mismatch")
        if review.category_name != result.category_name:
            raise ValueError("extraction review category mismatch")
        if review.page_id != result.page_id or review.revision_id != result.revision_id:
            raise ValueError("extraction review page revision mismatch")
        if review.source_id != decision.source_id:
            raise ValueError("extraction review source mismatch")
        if review.extraction_decision_sha256 != expected_digest:
            raise ValueError("extraction review digest mismatch")
        if path.name != review_filename(review.category_name, review.page_id, review.source_id):
            raise ValueError("extraction review filename mismatch")
        evidence = replace(
            decision.evidence,
            reason_detail=(
                f"{decision.evidence.reason_detail}; extraction_review={path.name}; "
                f"review_reason={review.reason}"
            ),
        )
        replacements[id(decision)] = replace(
            decision,
            disposition="excluded",
            reason_code="extraction_review_exclude",
            selection_reason=None,
            evidence=evidence,
        )

    return ExtractionResult(
        page_id=result.page_id,
        revision_id=result.revision_id,
        category_name=result.category_name,
        decisions=tuple(replacements.get(id(decision), decision) for decision in result.decisions),
    )
