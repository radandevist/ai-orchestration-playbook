from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import List


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MEDIUM = "MEDIUM"
    MINOR = "MINOR"
    NOTE = "NOTE"


class Disposition(StrEnum):
    BLOCKS_PR = "BLOCKS_PR"
    FOLLOW_UP_ISSUE = "FOLLOW_UP_ISSUE"
    NOTE_ONLY = "NOTE_ONLY"


class Verdict(StrEnum):
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    APPROVED_WITH_FOLLOW_UPS = "APPROVED_WITH_FOLLOW_UPS"
    APPROVED = "APPROVED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class Finding:
    id: str
    root_cause: str
    severity: Severity
    disposition: Disposition
    scope: str
    summary: str
    evidence: List[str]
    follow_up_issue: int | None = None


@dataclass(frozen=True)
class ReviewRecord:
    schema_version: int
    repository: str
    pr_number: int
    reviewed_commit: str
    implementer_family: str
    reviewer_family: str
    verdict: Verdict
    findings: List[Finding]
    intentionally_not_findings: List[str] = field(default_factory=list)