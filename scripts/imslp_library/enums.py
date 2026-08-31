from enum import Enum


class CategoryKind(str, Enum):
    ORIGINAL = "original"
    ARRANGEMENT = "arrangement"


class StorageMethod(str, Enum):
    HARDLINK = "hardlink"
    RELATIVE_SYMLINK = "relative_symlink"


class SelectionReason(str, Enum):
    EXACT_ORIGINAL_INSTRUMENTATION = "exact_original_instrumentation"
    EXACT_ARRANGEMENT_HEADING = "exact_arrangement_heading"
    WORK_LEVEL_EXACT_INSTRUMENTATION = "work_level_exact_instrumentation"


class RunStatus(str, Enum):
    SNAPSHOT_INCOMPLETE = "snapshot_incomplete"
    SNAPSHOT_COMPLETE = "snapshot_complete"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"


class AttemptPhase(str, Enum):
    STARTED = "started"
    STREAMING = "streaming"
    FINISHED = "finished"


class ReviewStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    RESOLVED = "resolved"


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DownloadStatus(str, Enum):
    NOT_STARTED = "not_started"
    RETRYABLE = "retryable"
    HUMAN_VERIFICATION_REQUIRED = "human_verification_required"
    MEMBERSHIP_WAIT_PENDING = "membership_wait_pending"
    COPYRIGHT_RESTRICTED = "copyright_restricted"
    REGION_RESTRICTED = "region_restricted"
    MEMBERSHIP_REQUIRED = "membership_required"
    LOGIN_REQUIRED = "login_required"
    COMMERCIAL_ONLY = "commercial_only"
    DELETED = "deleted"
    MANUAL_REVIEW = "manual_review"
    DOWNLOADED_VERIFIED = "downloaded_verified"
    SOURCE_OVERRIDE_VERIFIED = "source_override_verified"
