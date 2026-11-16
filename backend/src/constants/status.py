from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class PublishState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class SourceKind(StrEnum):
    MEASURED = "measured"
    REPLACEMENT = "replacement"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ReplacementState(StrEnum):
    NONE = "none"
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
