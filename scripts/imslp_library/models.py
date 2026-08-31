from __future__ import annotations

import hashlib
import math
import re
import types
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, get_args, get_origin, get_type_hints

from .enums import (
    AttemptPhase,
    DownloadStatus,
    IssueSeverity,
    ReviewStatus,
    RunStatus,
    SelectionReason,
    StorageMethod,
)

_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WORK_ID_RE = re.compile(r"work:p([0-9]+)@r([0-9]+)")
_SOURCE_ID_RE = re.compile(r"source:f(.+)@r([0-9]+)")
_MEMBERSHIP_ID_RE = re.compile(r"membership:([0-9a-f]{10}):(source:f.+@r[0-9]+)")
_MODEL_REGISTRY: dict[str, type[Model]] = {}


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _sha1(value: str | None, name: str) -> None:
    if value is not None and (not isinstance(value, str) or _SHA1_RE.fullmatch(value) is None):
        raise ValueError(f"{name} must be 40 lowercase hexadecimal characters")


def _sha256(value: str | None, name: str) -> None:
    if value is not None and (not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None):
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")


def _required_sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")


def _aware(value: datetime | None, name: str, *, optional: bool = False) -> None:
    if value is None:
        if optional:
            return
        raise ValueError(f"{name} must be timezone-aware")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _strings(value: tuple[str, ...], name: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, tuple) or (nonempty and not value):
        raise ValueError(f"{name} must be a{' nonempty' if nonempty else ''} tuple")
    for item in value:
        _nonempty(item, name)


def _validate_source_id(value: str, file_id: str | None = None, revision_id: int | None = None) -> re.Match[str]:
    _nonempty(value, "source_id")
    match = _SOURCE_ID_RE.fullmatch(value)
    if match is None:
        raise ValueError("source_id is not stable")
    if match.group(2) != str(int(match.group(2))):
        raise ValueError("source_id revision is not canonical")
    if file_id is not None and revision_id is not None:
        if value != f"source:f{file_id}@r{revision_id}":
            raise ValueError("source_id does not match file_id and page_revision_id")
    elif file_id is not None and match.group(1) != file_id:
        raise ValueError("source_id does not match file_id")
    elif revision_id is not None and match.group(2) != str(revision_id):
        raise ValueError("source_id does not match page_revision_id")
    return match


def _validate_work_id(value: str, page_id: int | None = None, revision_id: int | None = None) -> re.Match[str]:
    _nonempty(value, "work_id")
    match = _WORK_ID_RE.fullmatch(value)
    if match is None:
        raise ValueError("work_id is not stable")
    canonical = f"work:p{int(match.group(1))}@r{int(match.group(2))}"
    if value != canonical:
        raise ValueError("work_id is not canonical")
    if page_id is not None and revision_id is not None and value != f"work:p{page_id}@r{revision_id}":
        raise ValueError("work_id must match page_id and revision_id")
    return match


def _validate_membership_id(value: str) -> re.Match[str]:
    _nonempty(value, "membership_id")
    match = _MEMBERSHIP_ID_RE.fullmatch(value)
    if match is None:
        raise ValueError("membership_id is not stable")
    try:
        _validate_source_id(match.group(2))
    except ValueError as exc:
        raise ValueError("membership_id contains a noncanonical source_id") from exc
    return match


def _normalize_json(value: object, path: str = "value") -> object:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, f"{path}[]") for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError(f"{path} keys must be strings")
        return {key: _normalize_json(value[key], f"{path}.{key}") for key in sorted(value)}
    raise TypeError(f"{path} is not JSON serializable")


def _encode(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        _aware(value, "datetime")
        return value.isoformat()
    if isinstance(value, Model):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(value[key]) for key in sorted(value)}
    return value


def _decode(value: object, annotation: object, name: str) -> object:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (types.UnionType, getattr(__import__("typing"), "Union")):
        if type(None) in args and value is None:
            return None
        candidates = tuple(item for item in args if item is not type(None))
        errors: list[Exception] = []
        for candidate in candidates:
            try:
                return _decode(value, candidate, name)
            except (TypeError, ValueError) as exc:
                errors.append(exc)
        raise TypeError(f"{name} has the wrong type") from errors[-1] if errors else None
    if origin is tuple:
        if not isinstance(value, list):
            raise TypeError(f"{name} must be an array")
        item_type = args[0]
        return tuple(_decode(item, item_type, name) for item in value)
    if origin is dict:
        if not isinstance(value, dict):
            raise TypeError(f"{name} must be an object")
        if args[0] is not str or not all(isinstance(key, str) for key in value):
            raise TypeError(f"{name} keys must be strings")
        return {key: _decode(value[key], args[1], name) for key in sorted(value)}
    if annotation is object:
        return _normalize_json(value, name)
    if annotation is datetime:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be an ISO datetime")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO datetime") from exc
        _aware(parsed, name)
        return parsed
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be an enum string")
        return annotation(value)
    if isinstance(annotation, type) and issubclass(annotation, Model):
        if not isinstance(value, dict):
            raise TypeError(f"{name} must be a model object")
        return annotation.from_dict(value)
    if annotation is bool:
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean")
        return value
    if annotation is int:
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
        return value
    if annotation is str:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        return value
    raise TypeError(f"unsupported field type for {name}: {annotation!r}")


class Model:
    __slots__ = ()
    model_type: ClassVar[str]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        cls.model_type = cls.__name__
        _MODEL_REGISTRY[cls.__name__] = cls

    def to_dict(self) -> dict[str, object]:
        if not is_dataclass(self):
            raise TypeError("model must be a dataclass")
        payload = {field.name: _encode(getattr(self, field.name)) for field in fields(self)}
        payload["model_type"] = type(self).__name__
        return {key: payload[key] for key in sorted(payload)}

    @classmethod
    def from_dict(cls, payload: object):
        if not isinstance(payload, dict):
            raise TypeError("model payload must be an object")
        model_type = payload.get("model_type")
        if not isinstance(model_type, str):
            raise ValueError("missing model_type")
        if model_type not in _MODEL_REGISTRY:
            raise ValueError(f"unknown model_type: {model_type}")
        target = _MODEL_REGISTRY[model_type]
        if cls is not Model and target is not cls:
            raise ValueError(f"model_type must be {cls.__name__}")
        expected = {field.name for field in fields(target)} | {"model_type"}
        actual = set(payload)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(f"invalid keys; missing={missing}, extra={extra}")
        hints = get_type_hints(target)
        values = {field.name: _decode(payload[field.name], hints[field.name], field.name) for field in fields(target)}
        return target(**values)


def model_from_dict(payload: object) -> Model:
    return Model.from_dict(payload)


def model_class_for_name(model_type: str) -> type[Model]:
    if not isinstance(model_type, str) or model_type not in _MODEL_REGISTRY:
        raise ValueError(f"unknown model_type: {model_type}")
    return _MODEL_REGISTRY[model_type]


@dataclass(frozen=True, slots=True)
class FrozenPage(Model):
    page_id: int
    revision_id: int
    page_title: str
    category_names: tuple[str, ...]
    wikitext_path: str
    wikitext_sha256: str

    def __post_init__(self) -> None:
        _nonnegative(self.page_id, "page_id")
        _nonnegative(self.revision_id, "revision_id")
        _nonempty(self.page_title, "page_title")
        _strings(self.category_names, "category_names", nonempty=True)
        _nonempty(self.wikitext_path, "wikitext_path")
        _required_sha256(self.wikitext_sha256, "wikitext_sha256")


@dataclass(frozen=True, slots=True)
class Work(Model):
    work_id: str
    page_id: int
    revision_id: int
    page_title: str
    title_en: str
    composer_en: str
    imslp_url: str

    def __post_init__(self) -> None:
        _nonnegative(self.page_id, "page_id")
        _nonnegative(self.revision_id, "revision_id")
        for name in ("work_id", "page_title", "title_en", "composer_en", "imslp_url"):
            _nonempty(getattr(self, name), name)
        _validate_work_id(self.work_id, self.page_id, self.revision_id)


@dataclass(frozen=True, slots=True)
class ScoreFile(Model):
    source_id: str
    file_id: str
    page_id: int
    page_revision_id: int
    filename: str
    source_url: str
    expected_size: int | None
    sha1_imslp: str | None
    source_hash_missing: bool
    mime: str | None
    copyright_label: str | None
    sha256: str | None
    object_path: str | None

    def __post_init__(self) -> None:
        _nonempty(self.file_id, "file_id")
        _nonnegative(self.page_id, "page_id")
        _nonnegative(self.page_revision_id, "page_revision_id")
        _validate_source_id(self.source_id, self.file_id, self.page_revision_id)
        _nonempty(self.filename, "filename")
        _nonempty(self.source_url, "source_url")
        if self.expected_size is not None:
            _nonnegative(self.expected_size, "expected_size")
        _sha1(self.sha1_imslp, "sha1_imslp")
        _sha256(self.sha256, "sha256")
        if type(self.source_hash_missing) is not bool or self.source_hash_missing != (self.sha1_imslp is None):
            raise ValueError("source_hash_missing must reflect sha1_imslp absence")
        if (self.sha256 is None) != (self.object_path is None):
            raise ValueError("sha256 and object_path must be present together")
        for name in ("mime", "copyright_label", "object_path"):
            value = getattr(self, name)
            if value is not None:
                _nonempty(value, name)


@dataclass(frozen=True, slots=True)
class SelectionEvidence(Model):
    heading_raw: str | None
    heading_normalized: str | None
    heading_ancestry: tuple[str, ...]
    instrumentation_raw: str | None
    instrumentation_normalized: str | None
    branch: str
    reason_detail: str

    def __post_init__(self) -> None:
        for name in ("heading_raw", "heading_normalized", "instrumentation_raw", "instrumentation_normalized"):
            value = getattr(self, name)
            if value is not None:
                _nonempty(value, name)
        _strings(self.heading_ancestry, "heading_ancestry")
        _nonempty(self.branch, "branch")
        _nonempty(self.reason_detail, "reason_detail")


@dataclass(frozen=True, slots=True)
class Membership(Model):
    membership_id: str
    category_name: str
    work_id: str
    source_id: str
    selection_reason: SelectionReason
    evidence: SelectionEvidence
    planned_local_path: str
    local_path: str | None
    storage_method: StorageMethod | None
    active: bool

    def __post_init__(self) -> None:
        match = _validate_membership_id(self.membership_id)
        _nonempty(self.category_name, "category_name")
        if match.group(1) != hashlib.sha256(self.category_name.encode("utf-8")).hexdigest()[:10] or match.group(2) != self.source_id:
            raise ValueError("membership_id must match category_name and source_id")
        _validate_work_id(self.work_id)
        _validate_source_id(self.source_id)
        if not isinstance(self.selection_reason, SelectionReason) or not isinstance(self.evidence, SelectionEvidence):
            raise TypeError("selection_reason and evidence have invalid types")
        _nonempty(self.planned_local_path, "planned_local_path")
        if (self.local_path is None) != (self.storage_method is None):
            raise ValueError("local_path and storage_method must be present together")
        if self.local_path is not None:
            _nonempty(self.local_path, "local_path")
            if not isinstance(self.storage_method, StorageMethod):
                raise TypeError("storage_method must be StorageMethod")
        if type(self.active) is not bool:
            raise TypeError("active must be boolean")


@dataclass(frozen=True, slots=True)
class ExtractionDecision(Model):
    source_id: str | None
    filename: str
    disposition: str
    reason_code: str
    selection_reason: SelectionReason | None
    evidence: SelectionEvidence

    def __post_init__(self) -> None:
        if self.source_id is not None:
            _validate_source_id(self.source_id)
        _nonempty(self.filename, "filename")
        if self.disposition not in {"selected", "excluded", "manual_review"}:
            raise ValueError("invalid disposition")
        _nonempty(self.reason_code, "reason_code")
        if self.selection_reason is not None and not isinstance(self.selection_reason, SelectionReason):
            raise TypeError("selection_reason has invalid type")
        if not isinstance(self.evidence, SelectionEvidence):
            raise TypeError("evidence has invalid type")
        if self.disposition == "selected" and (self.source_id is None or self.selection_reason is None):
            raise ValueError("selected decisions require source_id and selection_reason")


@dataclass(frozen=True, slots=True)
class ExtractionResult(Model):
    page_id: int
    revision_id: int
    category_name: str
    decisions: tuple[ExtractionDecision, ...]

    def __post_init__(self) -> None:
        _nonnegative(self.page_id, "page_id")
        _nonnegative(self.revision_id, "revision_id")
        _nonempty(self.category_name, "category_name")
        if not isinstance(self.decisions, tuple) or not all(isinstance(item, ExtractionDecision) for item in self.decisions):
            raise TypeError("decisions must be a tuple of ExtractionDecision")

    @property
    def selected(self) -> tuple[ExtractionDecision, ...]:
        return tuple(item for item in self.decisions if item.disposition == "selected")

    @property
    def excluded(self) -> tuple[ExtractionDecision, ...]:
        return tuple(item for item in self.decisions if item.disposition == "excluded")

    @property
    def manual_review(self) -> tuple[ExtractionDecision, ...]:
        return tuple(item for item in self.decisions if item.disposition == "manual_review")


@dataclass(frozen=True, slots=True)
class ExtractionReview(Model):
    run_id: str
    category_name: str
    page_id: int
    revision_id: int
    source_id: str | None
    decision: str
    reason: str
    extraction_decision_sha256: str
    reviewed_at: datetime

    def __post_init__(self) -> None:
        for name in ("run_id", "category_name", "reason"):
            _nonempty(getattr(self, name), name)
        _nonnegative(self.page_id, "page_id")
        _nonnegative(self.revision_id, "revision_id")
        if self.source_id is not None:
            _validate_source_id(self.source_id, revision_id=self.revision_id)
        if self.decision != "exclude":
            raise ValueError("ExtractionReview decision must be exclude")
        _required_sha256(self.extraction_decision_sha256, "extraction_decision_sha256")
        _aware(self.reviewed_at, "reviewed_at")


@dataclass(frozen=True, slots=True)
class CategorySnapshot(Model):
    name: str
    member_page_ids: tuple[int, ...]
    member_count: int

    def __post_init__(self) -> None:
        _nonempty(self.name, "name")
        if not isinstance(self.member_page_ids, tuple) or any(type(item) is not int or item < 0 for item in self.member_page_ids):
            raise ValueError("member_page_ids must contain nonnegative integers")
        normalized = tuple(sorted(set(self.member_page_ids)))
        object.__setattr__(self, "member_page_ids", normalized)
        _nonnegative(self.member_count, "member_count")
        if self.member_count != len(normalized):
            raise ValueError("member_count must equal unique member_page_ids length")


@dataclass(frozen=True, slots=True)
class RunSnapshot(Model):
    schema_version: int
    run_id: str
    config_version: str
    config_sha256: str
    snapshot_started_at: datetime
    snapshot_completed_at: datetime | None
    status: RunStatus
    categories: tuple[CategorySnapshot, ...]
    pages: tuple[FrozenPage, ...]
    score_files: tuple[ScoreFile, ...]

    def __post_init__(self) -> None:
        _positive(self.schema_version, "schema_version")
        for name in ("run_id", "config_version"):
            _nonempty(getattr(self, name), name)
        _required_sha256(self.config_sha256, "config_sha256")
        _aware(self.snapshot_started_at, "snapshot_started_at")
        _aware(self.snapshot_completed_at, "snapshot_completed_at", optional=True)
        if not isinstance(self.status, RunStatus):
            raise TypeError("status must be RunStatus")
        if self.status is RunStatus.SNAPSHOT_COMPLETE and self.snapshot_completed_at is None:
            raise ValueError("complete snapshot requires snapshot_completed_at")
        if self.status is RunStatus.SNAPSHOT_INCOMPLETE and self.snapshot_completed_at is not None:
            raise ValueError("incomplete snapshot cannot have snapshot_completed_at")
        if self.snapshot_completed_at is not None and self.snapshot_completed_at < self.snapshot_started_at:
            raise ValueError("snapshot completion precedes start")
        for name, item_type, key in (("categories", CategorySnapshot, lambda x: x.name), ("pages", FrozenPage, lambda x: (x.page_id, x.revision_id)), ("score_files", ScoreFile, lambda x: x.source_id)):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(isinstance(item, item_type) for item in values):
                raise TypeError(f"{name} has invalid type")
            object.__setattr__(self, name, tuple(sorted(values, key=key)))


@dataclass(frozen=True, slots=True)
class RunState(Model):
    schema_version: int
    run_id: str
    snapshot_path: str
    snapshot_sha256: str | None
    start_drift_report_path: str | None
    start_drift_report_sha256: str | None
    end_drift_report_path: str | None
    end_drift_report_sha256: str | None
    download_attempt_manifest_path: str | None
    updated_at: datetime

    def __post_init__(self) -> None:
        _positive(self.schema_version, "schema_version")
        _nonempty(self.run_id, "run_id")
        _nonempty(self.snapshot_path, "snapshot_path")
        _sha256(self.snapshot_sha256, "snapshot_sha256")
        for stem in ("start_drift_report", "end_drift_report"):
            path = getattr(self, f"{stem}_path")
            digest = getattr(self, f"{stem}_sha256")
            _sha256(digest, f"{stem}_sha256")
            if (path is None) != (digest is None):
                raise ValueError(f"{stem} path and hash must be present together")
            if path is not None:
                _nonempty(path, f"{stem}_path")
        if self.download_attempt_manifest_path is not None:
            _nonempty(self.download_attempt_manifest_path, "download_attempt_manifest_path")
        if self.snapshot_sha256 is None and any(
            value is not None
            for value in (
                self.start_drift_report_path,
                self.start_drift_report_sha256,
                self.end_drift_report_path,
                self.end_drift_report_sha256,
                self.download_attempt_manifest_path,
            )
        ):
            raise ValueError("incomplete snapshot cannot reference drift or download artifacts")
        _aware(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class DownloadTarget(Model):
    category_name: str
    membership_id: str
    score: ScoreFile

    def __post_init__(self) -> None:
        _nonempty(self.category_name, "category_name")
        match = _validate_membership_id(self.membership_id)
        if not isinstance(self.score, ScoreFile):
            raise TypeError("score must be ScoreFile")
        if match.group(2) != self.score.source_id:
            raise ValueError("membership_id source must match score")
        if match.group(1) != hashlib.sha256(self.category_name.encode("utf-8")).hexdigest()[:10]:
            raise ValueError("membership_id category digest must match category_name")


def _association_pairs(category_names: tuple[str, ...], membership_ids: tuple[str, ...], source_id: str) -> None:
    _strings(category_names, "category_names", nonempty=True)
    _strings(membership_ids, "membership_ids", nonempty=True)
    if len(category_names) != len(membership_ids):
        raise ValueError("category and membership associations must have equal length")
    pairs = tuple(zip(category_names, membership_ids, strict=True))
    if len(set(pairs)) != len(pairs) or pairs != tuple(sorted(pairs)):
        raise ValueError("association pairs must be unique and stable sorted")
    for category_name, membership_id in pairs:
        match = _validate_membership_id(membership_id)
        if match.group(2) != source_id:
            raise ValueError("membership association source mismatch")
        if match.group(1) != hashlib.sha256(category_name.encode("utf-8")).hexdigest()[:10]:
            raise ValueError("membership association category mismatch")


@dataclass(frozen=True, slots=True)
class DownloadAttempt(Model):
    run_id: str
    attempt_number: int
    category_names: tuple[str, ...]
    membership_ids: tuple[str, ...]
    source_id: str
    phase: AttemptPhase
    status: DownloadStatus
    review_status: ReviewStatus
    started_at: datetime
    completed_at: datetime | None
    http_status: int | None
    evidence_path: str | None
    evidence_sha256: str | None
    part_path: str | None
    bytes_written: int
    retry_after: datetime | None
    detail_code: str

    def __post_init__(self) -> None:
        _nonempty(self.run_id, "run_id")
        _positive(self.attempt_number, "attempt_number")
        _validate_source_id(self.source_id)
        _association_pairs(self.category_names, self.membership_ids, self.source_id)
        if not isinstance(self.phase, AttemptPhase) or not isinstance(self.status, DownloadStatus) or not isinstance(self.review_status, ReviewStatus):
            raise TypeError("attempt enum field has invalid type")
        _aware(self.started_at, "started_at")
        _aware(self.completed_at, "completed_at", optional=True)
        _aware(self.retry_after, "retry_after", optional=True)
        _nonnegative(self.bytes_written, "bytes_written")
        _nonempty(self.detail_code, "detail_code")
        if self.http_status is not None:
            _nonnegative(self.http_status, "http_status")
        if (self.evidence_path is None) != (self.evidence_sha256 is None):
            raise ValueError("evidence_path and evidence_sha256 must be present together")
        _sha256(self.evidence_sha256, "evidence_sha256")
        for name in ("evidence_path", "part_path"):
            value = getattr(self, name)
            if value is not None:
                _nonempty(value, name)
        if self.phase in {AttemptPhase.STARTED, AttemptPhase.STREAMING}:
            if self.status is not DownloadStatus.NOT_STARTED or self.completed_at is not None:
                raise ValueError("unfinished attempts require not_started and no completion")
        elif self.status is DownloadStatus.NOT_STARTED or self.completed_at is None:
            raise ValueError("finished attempts require terminal status and completion")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("attempt completion precedes start")


@dataclass(frozen=True, slots=True)
class DownloadResult(Model):
    source_id: str
    status: DownloadStatus
    review_status: ReviewStatus
    review_evidence_path: str | None
    attempted_at: datetime
    retry_after: datetime | None
    http_status: int | None
    evidence_path: str | None
    detail: str
    size: int | None
    sha1: str | None
    sha256: str | None
    source_hash_missing: bool

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        if not isinstance(self.status, DownloadStatus) or not isinstance(self.review_status, ReviewStatus):
            raise TypeError("download result enum field has invalid type")
        _aware(self.attempted_at, "attempted_at")
        _aware(self.retry_after, "retry_after", optional=True)
        if self.http_status is not None:
            _nonnegative(self.http_status, "http_status")
        _nonempty(self.detail, "detail")
        if self.size is not None:
            _nonnegative(self.size, "size")
        _sha1(self.sha1, "sha1")
        _sha256(self.sha256, "sha256")
        if type(self.source_hash_missing) is not bool or self.source_hash_missing != (self.sha1 is None):
            raise ValueError("source_hash_missing must reflect sha1 absence")
        for name in ("review_evidence_path", "evidence_path"):
            value = getattr(self, name)
            if value is not None:
                _nonempty(value, name)
        if self.review_status is ReviewStatus.NOT_REQUIRED and self.review_evidence_path is not None:
            raise ValueError("not-required review cannot have review evidence")
        if self.review_status is ReviewStatus.PENDING and self.review_evidence_path is not None:
            raise ValueError("pending review cannot have review evidence")
        if self.review_status is ReviewStatus.RESOLVED and self.review_evidence_path is None:
            raise ValueError("resolved review requires review evidence")
        verified_statuses = {DownloadStatus.DOWNLOADED_VERIFIED, DownloadStatus.SOURCE_OVERRIDE_VERIFIED}
        no_payload_statuses = set(DownloadStatus) - verified_statuses - {DownloadStatus.MANUAL_REVIEW}
        if self.status in no_payload_statuses and any(value is not None for value in (self.size, self.sha1, self.sha256)):
            raise ValueError("non-success status cannot carry verified payload")
        if self.status in verified_statuses and (self.size is None or self.sha256 is None):
            raise ValueError("verified download requires size and sha256")
        if self.status is DownloadStatus.DOWNLOADED_VERIFIED:
            if self.source_hash_missing:
                if self.review_status not in {ReviewStatus.PENDING, ReviewStatus.RESOLVED}:
                    raise ValueError("missing source hash requires pending or resolved review")
                if self.review_status is ReviewStatus.PENDING and self.evidence_path is None:
                    raise ValueError("pending source hash review requires diagnostic evidence")
            elif self.review_status is not ReviewStatus.NOT_REQUIRED:
                raise ValueError("source-hashed download does not require review")


@dataclass(frozen=True, slots=True)
class DownloadBatchResult(Model):
    source_id: str
    category_names: tuple[str, ...]
    membership_ids: tuple[str, ...]
    result: DownloadResult

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        _association_pairs(self.category_names, self.membership_ids, self.source_id)
        if not isinstance(self.result, DownloadResult) or self.source_id != self.result.source_id:
            raise ValueError("batch source_id must match result.source_id")


@dataclass(frozen=True, slots=True)
class SourceHashReview(Model):
    source_id: str
    page_revision_id: int
    object_sha256: str
    decision: str
    reviewer_note: str
    reviewed_at: datetime

    def __post_init__(self) -> None:
        _nonnegative(self.page_revision_id, "page_revision_id")
        _validate_source_id(self.source_id, revision_id=self.page_revision_id)
        _required_sha256(self.object_sha256, "object_sha256")
        if self.decision not in {"accept_structural_without_source_hash", "reject"}:
            raise ValueError("invalid source hash review decision")
        _nonempty(self.reviewer_note, "reviewer_note")
        _aware(self.reviewed_at, "reviewed_at")


@dataclass(frozen=True, slots=True)
class StoredObject(Model):
    sha256: str
    size: int
    object_path: str
    sha1_imslp: str | None
    verified_at: datetime

    def __post_init__(self) -> None:
        _required_sha256(self.sha256, "sha256")
        _nonnegative(self.size, "size")
        _nonempty(self.object_path, "object_path")
        _sha1(self.sha1_imslp, "sha1_imslp")
        _aware(self.verified_at, "verified_at")


@dataclass(frozen=True, slots=True)
class MaterializationResult(Model):
    membership_id: str
    local_path: str
    storage_method: StorageMethod
    sha256: str

    def __post_init__(self) -> None:
        _validate_membership_id(self.membership_id)
        _nonempty(self.local_path, "local_path")
        if not isinstance(self.storage_method, StorageMethod):
            raise TypeError("storage_method must be StorageMethod")
        _required_sha256(self.sha256, "sha256")


@dataclass(frozen=True, slots=True)
class VerificationIssue(Model):
    code: str
    severity: IssueSeverity
    stable_ids: tuple[str, ...]
    evidence: dict[str, object]

    def __post_init__(self) -> None:
        _nonempty(self.code, "code")
        if not isinstance(self.severity, IssueSeverity):
            raise TypeError("severity must be IssueSeverity")
        _strings(self.stable_ids, "stable_ids")
        if not isinstance(self.evidence, dict):
            raise TypeError("evidence must be a dictionary")
        object.__setattr__(self, "evidence", _normalize_json(self.evidence, "evidence"))


@dataclass(frozen=True, slots=True)
class VerificationReport(Model):
    schema_version: int
    run_id: str
    scope: str
    scope_key: str
    verified_at: datetime
    facts: dict[str, object]
    issues: tuple[VerificationIssue, ...]
    complete: bool
    report_sha256: str

    def __post_init__(self) -> None:
        _positive(self.schema_version, "schema_version")
        for name in ("run_id", "scope", "scope_key"):
            _nonempty(getattr(self, name), name)
        _aware(self.verified_at, "verified_at")
        if not isinstance(self.facts, dict):
            raise TypeError("facts must be a dictionary")
        object.__setattr__(self, "facts", _normalize_json(self.facts, "facts"))
        if not isinstance(self.issues, tuple) or not all(isinstance(item, VerificationIssue) for item in self.issues):
            raise TypeError("issues must be a tuple of VerificationIssue")
        object.__setattr__(self, "issues", tuple(sorted(self.issues, key=lambda item: (item.severity.value, item.code, item.stable_ids))))
        if type(self.complete) is not bool:
            raise TypeError("complete must be boolean")
        _required_sha256(self.report_sha256, "report_sha256")
