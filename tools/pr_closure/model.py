from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional, Tuple


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


class ClosureState(StrEnum):
    CI_RED = "CI_RED"
    CI_INFRA_RETRY = "CI_INFRA_RETRY"
    FIXING = "FIXING"
    LOCAL_VERIFY = "LOCAL_VERIFY"
    REVIEW_READY = "REVIEW_READY"
    REVIEWING = "REVIEWING"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    DESIGN_RESET = "DESIGN_RESET"
    FOLLOW_UP_FILING = "FOLLOW_UP_FILING"
    APPROVED_WITH_FOLLOW_UPS = "APPROVED_WITH_FOLLOW_UPS"
    APPROVED = "APPROVED"
    NEEDS_OWNER = "NEEDS_OWNER"
    STALLED = "STALLED"
    UNVERIFIED = "UNVERIFIED"


class CiState(StrEnum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    PASSING = "PASSING"
    BRANCH_FAILURE = "BRANCH_FAILURE"
    INFRA_FAILURE = "INFRA_FAILURE"


class Evidence(StrEnum):
    WORKTREE = "worktree"
    LOCAL_COMMIT = "local_commit"
    REMOTE_COMMIT = "remote_commit"
    CI = "ci"
    CI_COMMIT = "ci_commit"
    DURABLE_TIP = "durable_tip"
    PR = "pr"
    HEAD_BRANCH = "head_branch"
    BASE_BRANCH = "base_branch"
    CHECKED_OUT_BRANCH = "checked_out_branch"


class Contradiction(StrEnum):
    LOCAL_REMOTE_MISMATCH = "local_remote_mismatch"
    CI_TIP_MISMATCH = "ci_tip_mismatch"
    REVIEW_TIP_MISMATCH = "review_tip_mismatch"
    DURABLE_TIP_MISMATCH = "durable_tip_mismatch"


ALL_EVIDENCE = frozenset(Evidence)


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


@dataclass(frozen=True)
class ProjectConfig:
    """Validated version-1 project closure configuration.

    ``repo_path`` and ``closure_state_dir`` are resolved durable absolute
    paths; command arrays keep the configured shell command strings verbatim.
    """

    schema_version: int
    project: str
    repository: str
    repo_path: str
    default_branch: str
    closure_state_dir: str
    local_review_ready_commands: Tuple[str, ...]
    closure_acceptance_commands: Tuple[str, ...]
    infra_retry_budget: int
    stagnation_budget_minutes: int
    heavy_job_limit: int
    verification_command_timeout_seconds: int
    tracking_projection: Optional[str]
    ci_required_checks: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(
            self, "local_review_ready_commands", _as_tuple(self.local_review_ready_commands)
        )
        object.__setattr__(
            self, "closure_acceptance_commands", _as_tuple(self.closure_acceptance_commands)
        )
        object.__setattr__(self, "ci_required_checks", _as_tuple(self.ci_required_checks))


@dataclass(frozen=True)
class ClosureSnapshot:
    """Already-read facts about one pull request, tied to one pushed tip.

    Defaults fail closed: missing evidence, unknown CI, absent commits, and
    unverified gates never imply a favorable state.
    """

    evidence_available: frozenset = frozenset()
    contradictions: frozenset = frozenset()
    local_commit: Optional[str] = None
    remote_commit: Optional[str] = None
    ci_commit: Optional[str] = None
    review_commit: Optional[str] = None
    verification_commit: Optional[str] = None
    durable_tip: Optional[str] = None
    head_branch: Optional[str] = None
    base_branch: Optional[str] = None
    checked_out_branch: Optional[str] = None
    pr_state: Optional[str] = None
    pr_is_draft: Optional[bool] = None
    worktree_clean: Optional[bool] = None
    local_verification: Optional[bool] = None
    ci_state: CiState = CiState.UNKNOWN
    fixing_lane_active: bool = False
    review_owned: bool = False
    review_verdict: Optional[Verdict] = None
    blocking_findings: Tuple[str, ...] = ()
    follow_up_findings: Tuple[str, ...] = ()
    follow_ups_complete: Optional[bool] = None
    repeated_root_cause: Optional[str] = None
    distinct_repair_strategies: int = 0
    executor_deaths: int = 0
    owner_decision_required: bool = False
    infra_retry_budget: int = 0
    infra_retries_used: int = 0
    stagnation_budget_minutes: int = 0
    last_progress_at: Optional[datetime] = None

    def __post_init__(self):
        object.__setattr__(self, "evidence_available", frozenset(self.evidence_available))
        object.__setattr__(self, "contradictions", frozenset(self.contradictions))
        object.__setattr__(self, "blocking_findings", _as_tuple(self.blocking_findings))
        object.__setattr__(self, "follow_up_findings", _as_tuple(self.follow_up_findings))


@dataclass(frozen=True)
class StateDecision:
    state: ClosureState
    reasons: Tuple[str, ...]
    allowed_actions: Tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "reasons", _as_tuple(self.reasons))
        object.__setattr__(self, "allowed_actions", _as_tuple(self.allowed_actions))
