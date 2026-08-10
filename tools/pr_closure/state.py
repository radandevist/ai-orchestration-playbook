from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Mapping, Tuple

from pr_closure.contract import COMMIT_ID_PATTERN, ReviewValidationError
from pr_closure.model import (
    CiState,
    ClosureSnapshot,
    ClosureState,
    Contradiction,
    Evidence,
    StateDecision,
    Verdict,
)

ALLOWED_ACTIONS: Mapping[ClosureState, Tuple[str, ...]] = {
    ClosureState.CI_RED: ("implement_or_verify_fix",),
    ClosureState.CI_INFRA_RETRY: ("rerun_failed_job",),
    ClosureState.FIXING: ("commit", "push", "verify"),
    ClosureState.LOCAL_VERIFY: ("run_missing_gate",),
    ClosureState.REVIEW_READY: ("dispatch_review",),
    ClosureState.REVIEWING: ("wait_for_verdict",),
    ClosureState.CHANGES_REQUIRED: ("create_fix_packet",),
    ClosureState.DESIGN_RESET: ("replace_mechanism", "verify", "re_review"),
    ClosureState.FOLLOW_UP_FILING: ("file_issues", "verify_issues"),
    ClosureState.APPROVED_WITH_FOLLOW_UPS: ("report_ready", "await_owner_merge"),
    ClosureState.APPROVED: ("report_ready", "await_owner_merge"),
    ClosureState.NEEDS_OWNER: ("ask_owner",),
    ClosureState.STALLED: ("rescue_lane", "redesign_packet", "move_to_owner"),
    ClosureState.UNVERIFIED: ("restore_evidence",),
}

_CORE_STATES_WITH_CI_COMMIT = (
    CiState.PASSING,
    CiState.BRANCH_FAILURE,
    CiState.INFRA_FAILURE,
)

_COMMIT_ID_RE = re.compile(COMMIT_ID_PATTERN)


def derive_state(snapshot, now) -> StateDecision:
    """Derive the fail-closed closure state from a pure snapshot.

    Pure: never touches Git, GitHub, Trello, subprocesses, the environment,
    or the filesystem. Malformed or unknown inputs raise rather than default.
    """
    _validate(snapshot, now)

    ci_state = snapshot.ci_state if isinstance(snapshot.ci_state, CiState) else CiState(snapshot.ci_state)
    review_verdict = snapshot.review_verdict
    if review_verdict is not None and not isinstance(review_verdict, Verdict):
        review_verdict = Verdict(review_verdict)

    missing = _missing_evidence(snapshot, ci_state, review_verdict)
    contradictions = _contradictions(snapshot)

    if missing or contradictions:
        return _decision(ClosureState.UNVERIFIED, tuple(missing) + tuple(contradictions))

    if snapshot.worktree_clean is False:
        return _decision(ClosureState.UNVERIFIED, ("dirty worktree",))

    if ci_state is CiState.BRANCH_FAILURE:
        return _decision(ClosureState.CI_RED, ("branch CI failure",))

    if ci_state is CiState.INFRA_FAILURE:
        if snapshot.infra_retries_used >= snapshot.infra_retry_budget:
            return _decision(ClosureState.NEEDS_OWNER, ("infrastructure retry budget exhausted",))
        return _decision(ClosureState.CI_INFRA_RETRY, ("proven infrastructure failure",))

    if snapshot.owner_decision_required:
        return _decision(ClosureState.NEEDS_OWNER, ("owner decision required",))

    if snapshot.executor_deaths >= 2:
        return _decision(ClosureState.STALLED, ("repeated executor deaths",))
    if (
        review_verdict not in (Verdict.APPROVED, Verdict.APPROVED_WITH_FOLLOW_UPS)
        and _stagnated(snapshot, now)
    ):
        return _decision(ClosureState.STALLED, ("no progress within stagnation budget",))

    if snapshot.fixing_lane_active and snapshot.blocking_findings:
        return _decision(ClosureState.FIXING, ("fix packet owns blocking findings",))

    if snapshot.local_verification is not True:
        reason = (
            "local verification failed"
            if snapshot.local_verification is False
            else "local verification incomplete"
        )
        return _decision(ClosureState.LOCAL_VERIFY, (reason,))
    if snapshot.verification_commit != snapshot.local_commit:
        return _decision(ClosureState.LOCAL_VERIFY, ("local verification stale",))

    if snapshot.review_owned and review_verdict is None and ci_state is CiState.PASSING:
        return _decision(ClosureState.REVIEWING, ("independent review in progress",))

    if snapshot.repeated_root_cause is not None and snapshot.distinct_repair_strategies >= 2:
        return _decision(
            ClosureState.DESIGN_RESET,
            ("repeated root cause survived two repair strategies",),
        )

    if snapshot.blocking_findings and ci_state is CiState.PASSING:
        return _decision(
            ClosureState.CHANGES_REQUIRED,
            ("blocking findings",) + tuple(snapshot.blocking_findings),
        )

    has_follow_ups = bool(snapshot.follow_up_findings) or review_verdict is Verdict.APPROVED_WITH_FOLLOW_UPS
    if has_follow_ups and snapshot.follow_ups_complete is not True:
        if ci_state is not CiState.PASSING:
            return _decision(ClosureState.UNVERIFIED, ("CI result pending",))
        return _decision(ClosureState.FOLLOW_UP_FILING, ("unfiled follow-up findings",))

    if ci_state is not CiState.PASSING:
        return _decision(ClosureState.UNVERIFIED, ("CI result pending",))

    if review_verdict is Verdict.APPROVED_WITH_FOLLOW_UPS and snapshot.follow_ups_complete is True:
        return _decision(
            ClosureState.APPROVED_WITH_FOLLOW_UPS,
            ("review approved with filed follow-ups",),
        )
    if review_verdict is Verdict.APPROVED and not snapshot.follow_up_findings:
        return _decision(ClosureState.APPROVED, ("review approved",))
    if review_verdict is not None:
        return _decision(ClosureState.UNVERIFIED, ("review outcome unresolved",))

    return _decision(ClosureState.REVIEW_READY, ("green tip awaiting review",))


def _decision(state: ClosureState, reasons: Tuple[str, ...]) -> StateDecision:
    return StateDecision(state, reasons, ALLOWED_ACTIONS[state])


def _missing_evidence(snapshot, ci_state, review_verdict) -> list:
    available = set(snapshot.evidence_available)
    missing = []
    if Evidence.WORKTREE not in available or snapshot.worktree_clean is None:
        missing.append("worktree evidence")
    if Evidence.LOCAL_COMMIT not in available or snapshot.local_commit is None:
        missing.append("local commit evidence")
    if Evidence.REMOTE_COMMIT not in available or snapshot.remote_commit is None:
        missing.append("remote commit evidence")
    if Evidence.CI not in available or ci_state is CiState.UNKNOWN:
        missing.append("CI result evidence")
    if ci_state in _CORE_STATES_WITH_CI_COMMIT and (
        Evidence.CI_COMMIT not in available or snapshot.ci_commit is None
    ):
        missing.append("CI commit evidence")
    if Evidence.DURABLE_TIP not in available or snapshot.durable_tip is None:
        missing.append("durable tip evidence")
    if Evidence.PR not in available or snapshot.pr_state is None or snapshot.pr_is_draft is None:
        missing.append("PR state evidence")
    if Evidence.HEAD_BRANCH not in available or snapshot.head_branch is None:
        missing.append("head branch evidence")
    if Evidence.BASE_BRANCH not in available or snapshot.base_branch is None:
        missing.append("base branch evidence")
    if Evidence.CHECKED_OUT_BRANCH not in available or snapshot.checked_out_branch is None:
        missing.append("checked-out branch evidence")
    if review_verdict is Verdict.INCONCLUSIVE:
        missing.append("conclusive review evidence")
    if (snapshot.review_owned or review_verdict is not None) and snapshot.review_commit is None:
        missing.append("reviewed commit evidence")
    if (snapshot.blocking_findings or snapshot.follow_up_findings) and review_verdict is None:
        missing.append("review verdict evidence")
    return missing


def _contradictions(snapshot) -> list:
    contradictions = sorted(snapshot.contradictions, key=str)
    if (
        snapshot.local_commit is not None
        and snapshot.remote_commit is not None
        and snapshot.local_commit != snapshot.remote_commit
    ):
        contradictions.append("unpushed commit")
    if (
        snapshot.local_commit is not None
        and snapshot.ci_commit is not None
        and snapshot.ci_commit != snapshot.local_commit
    ):
        contradictions.append("CI commit mismatch")
    if (
        snapshot.local_commit is not None
        and snapshot.review_commit is not None
        and snapshot.review_commit != snapshot.local_commit
    ):
        contradictions.append("reviewed commit mismatch")
    if (
        snapshot.local_commit is not None
        and snapshot.durable_tip is not None
        and snapshot.durable_tip != snapshot.local_commit
    ):
        contradictions.append("durable tip mismatch")
    if (
        snapshot.head_branch is not None
        and snapshot.checked_out_branch is not None
        and snapshot.head_branch != snapshot.checked_out_branch
    ):
        contradictions.append("checked-out branch mismatch")
    if snapshot.pr_state is not None and snapshot.pr_state != "OPEN":
        contradictions.append("pull request is not open")
    if snapshot.pr_is_draft is True:
        contradictions.append("draft pull request")
    return contradictions


def _stagnated(snapshot, now) -> bool:
    budget = snapshot.stagnation_budget_minutes
    if budget <= 0:
        return False
    if snapshot.last_progress_at is None:
        return True
    return now - snapshot.last_progress_at > timedelta(minutes=budget)


def _validate(snapshot, now) -> None:
    if not isinstance(snapshot, ClosureSnapshot):
        raise TypeError("snapshot must be a ClosureSnapshot")
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")

    for member in snapshot.evidence_available:
        if not isinstance(member, Evidence):
            Evidence(member)
    for member in snapshot.contradictions:
        if not isinstance(member, Contradiction):
            Contradiction(member)

    ci_state = snapshot.ci_state
    if not isinstance(ci_state, CiState):
        CiState(ci_state)
    review_verdict = snapshot.review_verdict
    if review_verdict is not None and not isinstance(review_verdict, Verdict):
        Verdict(review_verdict)

    for name, value in (
        ("worktree_clean", snapshot.worktree_clean),
        ("local_verification", snapshot.local_verification),
        ("follow_ups_complete", snapshot.follow_ups_complete),
        ("pr_is_draft", snapshot.pr_is_draft),
    ):
        if value is not None and not isinstance(value, bool):
            raise TypeError(f"{name} must be a bool or None")

    for name, value in (
        ("local_commit", snapshot.local_commit),
        ("remote_commit", snapshot.remote_commit),
        ("ci_commit", snapshot.ci_commit),
        ("review_commit", snapshot.review_commit),
        ("verification_commit", snapshot.verification_commit),
        ("durable_tip", snapshot.durable_tip),
        ("head_branch", snapshot.head_branch),
        ("base_branch", snapshot.base_branch),
        ("checked_out_branch", snapshot.checked_out_branch),
        ("pr_state", snapshot.pr_state),
        ("repeated_root_cause", snapshot.repeated_root_cause),
    ):
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{name} must be a str or None")

    for name, value in (
        ("local_commit", snapshot.local_commit),
        ("remote_commit", snapshot.remote_commit),
        ("ci_commit", snapshot.ci_commit),
        ("review_commit", snapshot.review_commit),
        ("verification_commit", snapshot.verification_commit),
        ("durable_tip", snapshot.durable_tip),
    ):
        if value is not None and _COMMIT_ID_RE.fullmatch(value) is None:
            raise ReviewValidationError(
                f"{name} must be a 40-character lowercase hex commit id"
            )

    for name, value in (
        ("fixing_lane_active", snapshot.fixing_lane_active),
        ("review_owned", snapshot.review_owned),
        ("owner_decision_required", snapshot.owner_decision_required),
    ):
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be a bool")

    for name, value in (
        ("blocking_findings", snapshot.blocking_findings),
        ("follow_up_findings", snapshot.follow_up_findings),
    ):
        if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{name} must be a tuple of str")

    for name, value in (
        ("infra_retry_budget", snapshot.infra_retry_budget),
        ("infra_retries_used", snapshot.infra_retries_used),
        ("distinct_repair_strategies", snapshot.distinct_repair_strategies),
        ("executor_deaths", snapshot.executor_deaths),
        ("stagnation_budget_minutes", snapshot.stagnation_budget_minutes),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} must be an int")
        if value < 0:
            raise ValueError(f"{name} must be nonnegative")

    if snapshot.last_progress_at is not None and not isinstance(snapshot.last_progress_at, datetime):
        raise TypeError("last_progress_at must be a datetime or None")
