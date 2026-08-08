from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import List, Optional, Tuple


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


def _as_tuple(value):
    return tuple(value) if not isinstance(value, tuple) else value


@dataclass(frozen=True)
class Finding:
    id: str
    root_cause: str
    severity: Severity
    disposition: Disposition
    scope: str
    summary: str
    evidence: Tuple[str, ...]
    follow_up_issue: Optional[int] = None
    bad_case_evidence: Tuple[str, ...] = ()
    good_case_evidence: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "evidence", _as_tuple(self.evidence))
        object.__setattr__(self, "bad_case_evidence", _as_tuple(self.bad_case_evidence))
        object.__setattr__(self, "good_case_evidence", _as_tuple(self.good_case_evidence))


@dataclass(frozen=True)
class ReviewRecord:
    schema_version: int
    repository: str
    pr_number: int
    reviewed_branch: str
    reviewed_commit: str
    implementer_family: str
    reviewer_family: str
    local_evidence: Tuple[str, ...]
    ci_evidence: Tuple[str, ...]
    verdict: Verdict
    findings: Tuple[Finding, ...]
    intentionally_not_findings: Tuple[str, ...] = ()
    base_commit: Optional[str] = None
    comparison_range: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "findings", _as_tuple(self.findings))
        object.__setattr__(self, "intentionally_not_findings", _as_tuple(self.intentionally_not_findings))
        object.__setattr__(self, "local_evidence", _as_tuple(self.local_evidence))
        object.__setattr__(self, "ci_evidence", _as_tuple(self.ci_evidence))