from enum import StrEnum


class Environment(StrEnum):
    TEST = "test"
    LIVE = "live"


class DocumentKind(StrEnum):
    ORIGINAL = "original"
    ADJUSTMENT = "adjustment"
    REPLACEMENT = "replacement"


class DocumentStatus(StrEnum):
    NEEDS_DRAFT = "needs_draft"
    BLOCKED = "blocked"
    QUEUED = "queued"
    PENDING_PROVIDER = "pending_provider"
    ISSUED = "issued"
    FAILED = "failed"
    REPLACED = "replaced"
    CANCELLED_EXTERNALLY = "cancelled_externally"


class DraftStatus(StrEnum):
    OPEN = "open"
    CONSUMED = "consumed"
    DISCARDED = "discarded"


class IssuerAccountStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class CrossDayAction(StrEnum):
    ISSUE = "issue"
    REVIEW = "review"


class BuyerSource(StrEnum):
    HOST = "host"
    FALLBACK = "fallback"


class CorrectionMode(StrEnum):
    ADJUST = "adjust"
    REPLACE = "replace"


class AdjustmentType(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    COMBINED = "combined"
    INFO = "info"


class IssueErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"
    ISSUED_DATE_INVALID = "issued_date_invalid"
    REGISTRATION_NOT_APPROVED = "registration_not_approved"
    QUOTA_EXHAUSTED = "quota_exhausted"
    SELLER_STORE_INVALID = "seller_store_invalid"
    DOCUMENT_EXISTED_UNRESOLVED = "document_existed_unresolved"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_TIMEOUT = "provider_timeout"
    AUTH_FAILED = "auth_failed"
    TRANSIENT_ERROR = "transient_error"
    MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
    CORRECTION_REJECTED = "correction_rejected"
    ARTIFACT_FETCH_FAILED = "artifact_fetch_failed"
    LOOKUP_MISMATCH = "lookup_mismatch"


class ReviewReason(StrEnum):
    DRAFT_MISSING = "DRAFT_MISSING"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    DUPLICATE_SETTLEMENT = "DUPLICATE_SETTLEMENT"
    ISSUER_ACCOUNT_UNAVAILABLE = "ISSUER_ACCOUNT_UNAVAILABLE"
    ISSUE_DATE_CROSSED_DAY = "ISSUE_DATE_CROSSED_DAY"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_FAILED = "AUTH_FAILED"
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    DOCUMENT_EXISTED_UNRESOLVED = "DOCUMENT_EXISTED_UNRESOLVED"
    CORRECTION_REJECTED = "CORRECTION_REJECTED"
    LOOKUP_MISMATCH = "LOOKUP_MISMATCH"
    STALE_REFERENCE_FOUND = "STALE_REFERENCE_FOUND"


class ReviewStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ReviewResolution(StrEnum):
    RETRY = "retry"
    RETRY_WITH_NEW_REFERENCE = "retry_with_new_reference"
    MARK_EXTERNAL = "mark_external"
    ATTACH_DRAFT = "attach_draft"
    WAIVE = "waive"
    ACKNOWLEDGE = "acknowledge"


ACKNOWLEDGEABLE_REASONS = frozenset(
    {
        ReviewReason.DUPLICATE_SETTLEMENT,
        ReviewReason.QUOTA_EXHAUSTED,
        ReviewReason.STALE_REFERENCE_FOUND,
    }
)
NOTE_REQUIRED_RESOLUTIONS = frozenset(
    {
        ReviewResolution.WAIVE,
        ReviewResolution.ACKNOWLEDGE,
        ReviewResolution.RETRY_WITH_NEW_REFERENCE,
    }
)
