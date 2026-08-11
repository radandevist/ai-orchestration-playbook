import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from pr_closure.model import (
    ALL_EVIDENCE,
    CiState,
    ClosureSnapshot,
    ClosureState,
    Contradiction,
    Evidence,
    MergeableState,
    StateDecision,
    Verdict,
)
from pr_closure.state import ALLOWED_ACTIONS, derive_state

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
NOW = datetime(2026, 8, 8, 12, 0, 0)


def review_ready():
    return ClosureSnapshot(
        evidence_available=ALL_EVIDENCE,
        local_commit=COMMIT_A,
        remote_commit=COMMIT_A,
        ci_commit=COMMIT_A,
        verification_commit=COMMIT_A,
        durable_tip=COMMIT_A,
        worktree_clean=True,
        local_verification=True,
        ci_state=CiState.PASSING,
        review_owned=False,
        review_verdict=None,
        review_commit=None,
        head_branch="feature/close",
        base_branch="develop",
        checked_out_branch="feature/close",
        pr_state="OPEN",
        pr_is_draft=False,
    )


def approved_snapshot():
    return replace(
        review_ready(),
        review_verdict=Verdict.APPROVED,
        review_commit=COMMIT_A,
        follow_up_findings=(),
        follow_ups_complete=True,
    )


class StateDerivationTests(unittest.TestCase):
    def test_branch_ci_failure_wins_over_review(self):
        snap = replace(
            review_ready(),
            ci_state=CiState.BRANCH_FAILURE,
            review_owned=True,
            review_commit=COMMIT_A,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.CI_RED, decision.state)
        self.assertEqual(("branch CI failure",), decision.reasons)
        self.assertEqual(ALLOWED_ACTIONS[ClosureState.CI_RED], decision.allowed_actions)

    def test_conflict_state_reaches_resolution_gate(self):
        snap = replace(review_ready(), merge_state_status="CONFLICTING")
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.NEEDS_RESOLUTION, decision.state)
        self.assertIn("resolve_merge_conflict", decision.allowed_actions)

    def test_mergeable_conflict_reaches_resolution_gate(self):
        snap = replace(
            review_ready(),
            mergeable=MergeableState.CONFLICTING,
            merge_state_status="DIRTY",
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.NEEDS_RESOLUTION, decision.state)
        self.assertIn("resolve_merge_conflict", decision.allowed_actions)

    def test_unknown_merge_state_does_not_block_approved_tip(self):
        snap = replace(approved_snapshot(), merge_state_status="UNKNOWN")
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.APPROVED, decision.state)

    def test_infra_failure_enters_bounded_retry(self):
        snap = replace(
            review_ready(),
            ci_state=CiState.INFRA_FAILURE,
            infra_retry_budget=2,
            infra_retries_used=0,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.CI_INFRA_RETRY, decision.state)
        self.assertEqual(("rerun_failed_job",), decision.allowed_actions)

    def test_unpushed_commit_is_unverified(self):
        snap = replace(review_ready(), remote_commit=COMMIT_B)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("unpushed commit", decision.reasons)

    def test_dirty_review_probe_is_unverified(self):
        snap = replace(review_ready(), worktree_clean=False)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("dirty worktree", decision.reasons)

    def test_green_unreviewed_tip_is_review_ready(self):
        decision = derive_state(review_ready(), NOW)
        self.assertEqual(ClosureState.REVIEW_READY, decision.state)
        self.assertEqual(("dispatch_review",), decision.allowed_actions)

    def test_reviewed_tip_mismatch_invalidates_approval(self):
        snap = replace(approved_snapshot(), review_commit=COMMIT_B)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("reviewed commit mismatch", decision.reasons)

    def test_blocking_finding_requires_changes(self):
        snap = replace(approved_snapshot(), blocking_findings=("F-1",))
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.CHANGES_REQUIRED, decision.state)
        self.assertIn("F-1", decision.reasons)

    def test_missing_follow_up_issue_requires_filing(self):
        snap = replace(
            review_ready(),
            review_verdict=Verdict.APPROVED_WITH_FOLLOW_UPS,
            review_commit=COMMIT_A,
            follow_up_findings=("F-2",),
            follow_ups_complete=False,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.FOLLOW_UP_FILING, decision.state)
        self.assertIn("file_issues", decision.allowed_actions)

    def test_approved_with_issues_requires_same_green_tip(self):
        snap = replace(
            review_ready(),
            review_verdict=Verdict.APPROVED_WITH_FOLLOW_UPS,
            review_commit=COMMIT_A,
            follow_up_findings=("F-2",),
            follow_ups_complete=True,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.APPROVED_WITH_FOLLOW_UPS, decision.state)
        self.assertEqual(review_ready().local_commit, snap.review_commit)

    def test_repeated_root_cause_enters_design_reset(self):
        snap = replace(
            review_ready(),
            repeated_root_cause="missing-invariant",
            distinct_repair_strategies=2,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.DESIGN_RESET, decision.state)

    def test_no_progress_inside_budget_enters_stalled(self):
        snap = replace(
            review_ready(),
            stagnation_budget_minutes=120,
            last_progress_at=NOW - timedelta(minutes=121),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.STALLED, decision.state)

    def test_approved_pr_does_not_stall_while_awaiting_owner_merge(self):
        snap = replace(
            approved_snapshot(),
            stagnation_budget_minutes=120,
            last_progress_at=NOW - timedelta(minutes=121),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.APPROVED, decision.state)
        self.assertEqual(("report_ready", "await_owner_merge"), decision.allowed_actions)

    def test_approved_with_follow_ups_does_not_stall_while_awaiting_owner_merge(self):
        snap = replace(
            review_ready(),
            review_verdict=Verdict.APPROVED_WITH_FOLLOW_UPS,
            review_commit=COMMIT_A,
            follow_up_findings=("F-1",),
            follow_ups_complete=True,
            stagnation_budget_minutes=120,
            last_progress_at=NOW - timedelta(minutes=121),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.APPROVED_WITH_FOLLOW_UPS, decision.state)
        self.assertEqual(("report_ready", "await_owner_merge"), decision.allowed_actions)


class AdversarialPrecedenceTests(unittest.TestCase):
    def test_contradictory_evidence_beats_red_ci(self):
        snap = replace(
            review_ready(),
            ci_state=CiState.BRANCH_FAILURE,
            contradictions=frozenset({Contradiction.CI_TIP_MISMATCH}),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)

    def test_dirty_probe_beats_apparent_approval(self):
        snap = replace(approved_snapshot(), worktree_clean=False)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("dirty worktree", decision.reasons)

    def test_branch_failure_beats_expired_stagnation(self):
        snap = replace(
            review_ready(),
            ci_state=CiState.BRANCH_FAILURE,
            stagnation_budget_minutes=120,
            last_progress_at=NOW - timedelta(minutes=121),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.CI_RED, decision.state)

    def test_exhausted_infra_retries_need_owner(self):
        snap = replace(
            review_ready(),
            ci_state=CiState.INFRA_FAILURE,
            infra_retry_budget=1,
            infra_retries_used=1,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.NEEDS_OWNER, decision.state)
        self.assertEqual(("ask_owner",), decision.allowed_actions)

    def test_two_executor_deaths_stall_even_with_active_fixing_lane(self):
        snap = replace(
            review_ready(),
            executor_deaths=2,
            fixing_lane_active=True,
            blocking_findings=("F-1",),
            review_verdict=Verdict.CHANGES_REQUIRED,
            review_commit=COMMIT_A,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.STALLED, decision.state)
        self.assertIn("rescue_lane", decision.allowed_actions)

    def test_active_review_precedes_repeated_root_decision(self):
        snap = replace(
            review_ready(),
            review_owned=True,
            review_commit=COMMIT_A,
            repeated_root_cause="missing-invariant",
            distinct_repair_strategies=2,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.REVIEWING, decision.state)

    def test_repeated_root_precedes_ordinary_blockers(self):
        snap = replace(
            review_ready(),
            blocking_findings=("F-1",),
            review_verdict=Verdict.CHANGES_REQUIRED,
            review_commit=COMMIT_A,
            repeated_root_cause="missing-invariant",
            distinct_repair_strategies=2,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.DESIGN_RESET, decision.state)

    def test_blocker_precedes_follow_up_filing(self):
        snap = replace(
            review_ready(),
            review_verdict=Verdict.CHANGES_REQUIRED,
            review_commit=COMMIT_A,
            blocking_findings=("F-1",),
            follow_up_findings=("F-2",),
            follow_ups_complete=False,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.CHANGES_REQUIRED, decision.state)

    def test_stale_evidence_cannot_approve(self):
        stale_review = replace(approved_snapshot(), review_commit=COMMIT_B)
        stale_ci = replace(approved_snapshot(), ci_commit=COMMIT_B)
        stale_local = replace(approved_snapshot(), verification_commit=COMMIT_B)
        for snap in (stale_review, stale_ci):
            decision = derive_state(snap, NOW)
            self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        decision = derive_state(stale_local, NOW)
        self.assertNotEqual(ClosureState.APPROVED, decision.state)
        self.assertEqual(ClosureState.LOCAL_VERIFY, decision.state)

    def test_inconclusive_cannot_approve(self):
        snap = replace(
            review_ready(),
            review_verdict=Verdict.INCONCLUSIVE,
            review_commit=COMMIT_A,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)

    def test_pending_ci_cannot_be_review_ready(self):
        snap = replace(review_ready(), ci_state=CiState.PENDING, ci_commit=None)
        decision = derive_state(snap, NOW)
        self.assertNotEqual(ClosureState.REVIEW_READY, decision.state)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)

    def test_owner_decision_cannot_override_red_ci(self):
        snap = replace(
            review_ready(),
            ci_state=CiState.BRANCH_FAILURE,
            owner_decision_required=True,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.CI_RED, decision.state)

    def test_missing_evidence_beats_ci_red(self):
        snap = replace(
            review_ready(),
            ci_state=CiState.BRANCH_FAILURE,
            evidence_available=frozenset({Evidence.WORKTREE, Evidence.LOCAL_COMMIT}),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("CI result evidence", decision.reasons)

    def test_all_states_and_actions_are_deterministic_tuples(self):
        expected_states = {
            "NEEDS_RESOLUTION",
            "CI_RED",
            "CI_INFRA_RETRY",
            "FIXING",
            "LOCAL_VERIFY",
            "REVIEW_READY",
            "REVIEWING",
            "CHANGES_REQUIRED",
            "DESIGN_RESET",
            "FOLLOW_UP_FILING",
            "APPROVED_WITH_FOLLOW_UPS",
            "APPROVED",
            "NEEDS_OWNER",
            "STALLED",
            "UNVERIFIED",
        }
        self.assertEqual(expected_states, set(ClosureState.__members__))
        self.assertEqual(expected_states, set(ALLOWED_ACTIONS))
        for state in ClosureState:
            actions = ALLOWED_ACTIONS[state]
            self.assertIsInstance(actions, tuple)
            self.assertTrue(all(isinstance(action, str) for action in actions))
        decision = derive_state(approved_snapshot(), NOW)
        self.assertIsInstance(decision, StateDecision)
        self.assertIsInstance(decision.reasons, tuple)
        self.assertIsInstance(decision.allowed_actions, tuple)
        self.assertEqual(decision, derive_state(approved_snapshot(), NOW))


class FailClosedValidationTests(unittest.TestCase):
    def test_malformed_enum_raises(self):
        snap = replace(review_ready(), ci_state="NOT_A_STATE")
        with self.assertRaises(ValueError):
            derive_state(snap, NOW)

    def test_unknown_mergeable_enum_raises(self):
        snap = replace(review_ready(), mergeable="MAYBE")
        with self.assertRaises(ValueError):
            derive_state(snap, NOW)

    def test_negative_budget_raises(self):
        snap = replace(review_ready(), infra_retries_used=-1)
        with self.assertRaises(ValueError):
            derive_state(snap, NOW)

    def test_unknown_evidence_key_raises(self):
        snap = replace(review_ready(), evidence_available=frozenset({"bogus"}))
        with self.assertRaises(ValueError):
            derive_state(snap, NOW)

    def test_missing_evidence_defaults_fail_closed(self):
        decision = derive_state(ClosureSnapshot(), NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)


class CommitShapeValidationTests(unittest.TestCase):
    """T2R-F1: every non-None commit must satisfy the Task 1 exact
    40-lowercase-hex contract; malformed shapes raise, never approve."""

    COMMIT_FIELDS = (
        "local_commit",
        "remote_commit",
        "ci_commit",
        "review_commit",
        "verification_commit",
        "durable_tip",
    )
    BAD_COMMITS = (
        "",
        "   ",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
    )

    def test_each_commit_field_rejects_every_malformed_shape(self):
        for field in self.COMMIT_FIELDS:
            for bad in self.BAD_COMMITS:
                with self.subTest(field=field, value=bad):
                    snap = replace(approved_snapshot(), **{field: bad})
                    with self.assertRaises(ValueError):
                        derive_state(snap, NOW)

    def test_empty_commit_never_approves(self):
        for field in self.COMMIT_FIELDS:
            with self.subTest(field=field):
                snap = replace(approved_snapshot(), **{field: ""})
                with self.assertRaises(ValueError):
                    derive_state(snap, NOW)


class FailClosedClauseTests(unittest.TestCase):
    """T2R-F2: each fail-closed clause is load-bearing; deleting it must fail
    the suite on the exact state/reason/action assertion."""

    def test_worktree_clean_unknown_is_unverified(self):
        snap = replace(approved_snapshot(), worktree_clean=None)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("worktree evidence", decision.reasons)

    def test_worktree_evidence_key_absent_is_unverified(self):
        snap = replace(
            approved_snapshot(),
            evidence_available=frozenset(ALL_EVIDENCE - {Evidence.WORKTREE}),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("worktree evidence", decision.reasons)

    def test_local_commit_missing_is_unverified(self):
        snap = replace(approved_snapshot(), local_commit=None, verification_commit=None)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("local commit evidence", decision.reasons)

    def test_remote_commit_missing_is_unverified(self):
        snap = replace(approved_snapshot(), remote_commit=None)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("remote commit evidence", decision.reasons)

    def test_ci_result_unknown_is_unverified(self):
        snap = replace(review_ready(), ci_state=CiState.UNKNOWN)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("CI result evidence", decision.reasons)

    def test_ci_commit_missing_is_unverified(self):
        snap = replace(approved_snapshot(), ci_commit=None)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("CI commit evidence", decision.reasons)

    def test_reviewed_commit_missing_is_unverified(self):
        snap = replace(approved_snapshot(), review_commit=None)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("reviewed commit evidence", decision.reasons)

    def test_findings_without_verdict_are_unverified(self):
        for label, field, value in (
            ("blocking", "blocking_findings", ("F-1",)),
            ("follow-up", "follow_up_findings", ("F-2",)),
        ):
            with self.subTest(kind=label):
                snap = replace(review_ready(), **{field: value})
                decision = derive_state(snap, NOW)
                self.assertEqual(ClosureState.UNVERIFIED, decision.state)
                self.assertIn("review verdict evidence", decision.reasons)

    def test_changes_required_without_blockers_is_unverified(self):
        snap = replace(
            review_ready(),
            review_verdict=Verdict.CHANGES_REQUIRED,
            review_commit=COMMIT_A,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("review outcome unresolved", decision.reasons)

    def test_unknown_local_verification_is_local_verify(self):
        snap = replace(review_ready(), local_verification=None)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.LOCAL_VERIFY, decision.state)
        self.assertEqual(("local verification incomplete",), decision.reasons)
        self.assertEqual(("run_missing_gate",), decision.allowed_actions)

    def test_owner_decision_requires_owner(self):
        snap = replace(review_ready(), owner_decision_required=True)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.NEEDS_OWNER, decision.state)
        self.assertEqual(("owner decision required",), decision.reasons)
        self.assertEqual(("ask_owner",), decision.allowed_actions)

    def test_invalid_now_raises(self):
        with self.assertRaises(TypeError):
            derive_state(review_ready(), "not-a-datetime")

    def test_absent_progress_under_positive_budget_stalls(self):
        snap = replace(
            review_ready(),
            stagnation_budget_minutes=120,
            last_progress_at=None,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.STALLED, decision.state)
        self.assertEqual(("no progress within stagnation budget",), decision.reasons)
        self.assertEqual(("rescue_lane", "redesign_packet", "move_to_owner"), decision.allowed_actions)

    def test_approved_verdict_with_unfiled_follow_ups_cannot_approve(self):
        snap = replace(approved_snapshot(), follow_up_findings=("F-2",))
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("review outcome unresolved", decision.reasons)

    def test_unknown_follow_up_completeness_requires_filing(self):
        snap = replace(
            review_ready(),
            review_verdict=Verdict.APPROVED_WITH_FOLLOW_UPS,
            review_commit=COMMIT_A,
            follow_up_findings=("F-2",),
            follow_ups_complete=None,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.FOLLOW_UP_FILING, decision.state)
        self.assertEqual(("unfiled follow-up findings",), decision.reasons)
        self.assertEqual(("file_issues", "verify_issues"), decision.allowed_actions)

    def test_non_snapshot_raises(self):
        with self.assertRaises(TypeError):
            derive_state("not-a-snapshot", NOW)

    def test_unknown_contradiction_key_raises(self):
        snap = replace(review_ready(), contradictions=frozenset({"bogus"}))
        with self.assertRaises(ValueError):
            derive_state(snap, NOW)

    def test_non_bool_tristate_raises(self):
        for field in ("worktree_clean", "local_verification", "follow_ups_complete"):
            with self.subTest(field=field):
                snap = replace(review_ready(), **{field: "yes"})
                with self.assertRaises(TypeError):
                    derive_state(snap, NOW)

    def test_non_str_commit_or_root_cause_raises(self):
        for field in (
            "local_commit",
            "remote_commit",
            "ci_commit",
            "review_commit",
            "verification_commit",
            "repeated_root_cause",
        ):
            with self.subTest(field=field):
                snap = replace(review_ready(), **{field: 123})
                with self.assertRaises(TypeError):
                    derive_state(snap, NOW)

    def test_findings_tuple_must_contain_only_strs(self):
        for field in ("blocking_findings", "follow_up_findings"):
            with self.subTest(field=field):
                snap = replace(review_ready(), **{field: ("F-1", 42)})
                with self.assertRaises(TypeError):
                    derive_state(snap, NOW)

    def test_non_bool_flags_raise(self):
        for field in ("fixing_lane_active", "review_owned", "owner_decision_required"):
            with self.subTest(field=field):
                snap = replace(review_ready(), **{field: 1})
                with self.assertRaises(TypeError):
                    derive_state(snap, NOW)


class TerminalStateAssertions(unittest.TestCase):
    """T2R-F2: the terminal states are reachable and pinned positively."""

    def test_approved_is_reachable(self):
        decision = derive_state(approved_snapshot(), NOW)
        self.assertEqual(ClosureState.APPROVED, decision.state)
        self.assertEqual(("review approved",), decision.reasons)
        self.assertEqual(("report_ready", "await_owner_merge"), decision.allowed_actions)

    def test_fixing_is_reachable(self):
        snap = replace(
            review_ready(),
            fixing_lane_active=True,
            blocking_findings=("F-1",),
            review_verdict=Verdict.CHANGES_REQUIRED,
            review_commit=COMMIT_A,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.FIXING, decision.state)
        self.assertEqual(("fix packet owns blocking findings",), decision.reasons)
        self.assertEqual(("commit", "push", "verify"), decision.allowed_actions)

    def test_local_verify_failed_reason_is_distinct(self):
        snap = replace(review_ready(), local_verification=False)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.LOCAL_VERIFY, decision.state)
        self.assertEqual(("local verification failed",), decision.reasons)

    def test_local_verify_stale_reason_is_distinct(self):
        snap = replace(review_ready(), verification_commit=COMMIT_B)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.LOCAL_VERIFY, decision.state)
        self.assertEqual(("local verification stale",), decision.reasons)


class BoundaryAssertionTests(unittest.TestCase):
    """T2R-F2: exact thresholds for design-reset, executor deaths and stagnation."""

    def test_single_repair_strategy_does_not_reset(self):
        snap = replace(
            review_ready(),
            repeated_root_cause="missing-invariant",
            distinct_repair_strategies=1,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.REVIEW_READY, decision.state)

    def test_two_repair_strategies_reset_design(self):
        snap = replace(
            review_ready(),
            repeated_root_cause="missing-invariant",
            distinct_repair_strategies=2,
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.DESIGN_RESET, decision.state)

    def test_single_executor_death_does_not_stall(self):
        snap = replace(review_ready(), executor_deaths=1)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.REVIEW_READY, decision.state)

    def test_stagnation_at_exact_budget_does_not_stall(self):
        snap = replace(
            review_ready(),
            stagnation_budget_minutes=120,
            last_progress_at=NOW - timedelta(minutes=120),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.REVIEW_READY, decision.state)

    def test_blocking_findings_on_pending_ci_cannot_route_changes_required(self):
        snap = replace(
            review_ready(),
            ci_state=CiState.PENDING,
            ci_commit=None,
            review_verdict=Verdict.CHANGES_REQUIRED,
            review_commit=COMMIT_A,
            blocking_findings=("F-1",),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("CI result pending", decision.reasons)


class DurableTipSeamTests(unittest.TestCase):
    """T6 seam: the CLI binds every durable record to one observed tip
    (Evidence.DURABLE_TIP / Contradiction.DURABLE_TIP_MISMATCH). Missing or
    mismatched durable-tip evidence must never approve."""

    def test_missing_durable_tip_is_unverified(self):
        snap = replace(review_ready(), durable_tip=None)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("durable tip evidence", decision.reasons)

    def test_durable_tip_evidence_key_absent_is_unverified(self):
        snap = replace(
            review_ready(),
            evidence_available=frozenset(ALL_EVIDENCE - {Evidence.DURABLE_TIP}),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("durable tip evidence", decision.reasons)

    def test_durable_tip_mismatch_is_unverified(self):
        snap = replace(review_ready(), durable_tip=COMMIT_B)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("durable tip mismatch", decision.reasons)

    def test_durable_tip_matching_tip_approves(self):
        decision = derive_state(approved_snapshot(), NOW)
        self.assertEqual(ClosureState.APPROVED, decision.state)

    def test_durable_tip_contradiction_beats_apparent_approval(self):
        snap = replace(
            approved_snapshot(),
            durable_tip=COMMIT_B,
            contradictions=frozenset({Contradiction.DURABLE_TIP_MISMATCH}),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)

    def test_non_str_durable_tip_raises(self):
        snap = replace(review_ready(), durable_tip=123)
        with self.assertRaises(TypeError):
            derive_state(snap, NOW)

    def test_malformed_durable_tip_raises(self):
        for bad in ("", "   ", "a" * 39, "A" * 40, "g" * 40):
            with self.subTest(value=bad):
                snap = replace(review_ready(), durable_tip=bad)
                with self.assertRaises(ValueError):
                    derive_state(snap, NOW)


class LifecycleFactPinsTests(unittest.TestCase):
    """C6C-F8: head/base/checked-out branch, PR state, and draft status are
    pinned before any terminal approval; every fact has a contradiction test."""

    def test_draft_pr_never_derives_approval(self):
        snap = replace(approved_snapshot(), pr_is_draft=True)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("draft pull request", decision.reasons)

    def test_non_open_pr_is_unverified(self):
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state):
                snap = replace(approved_snapshot(), pr_state=state)
                decision = derive_state(snap, NOW)
                self.assertEqual(ClosureState.UNVERIFIED, decision.state)
                self.assertIn("pull request is not open", decision.reasons)

    def test_checked_out_branch_mismatch_is_unverified(self):
        snap = replace(approved_snapshot(), checked_out_branch="other/branch")
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("checked-out branch mismatch", decision.reasons)

    def test_head_branch_mismatch_is_unverified(self):
        snap = replace(approved_snapshot(), head_branch="other/head")
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)

    def test_missing_pr_state_evidence_is_unverified(self):
        snap = replace(approved_snapshot(), pr_state=None, pr_is_draft=None)
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("PR state evidence", decision.reasons)

    def test_missing_head_branch_evidence_is_unverified(self):
        snap = replace(
            approved_snapshot(),
            head_branch=None,
            evidence_available=frozenset(ALL_EVIDENCE - {Evidence.HEAD_BRANCH}),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("head branch evidence", decision.reasons)

    def test_missing_base_branch_evidence_is_unverified(self):
        snap = replace(
            approved_snapshot(),
            base_branch=None,
            evidence_available=frozenset(ALL_EVIDENCE - {Evidence.BASE_BRANCH}),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("base branch evidence", decision.reasons)

    def test_missing_checked_out_branch_evidence_is_unverified(self):
        snap = replace(
            approved_snapshot(),
            checked_out_branch=None,
            evidence_available=frozenset(ALL_EVIDENCE - {Evidence.CHECKED_OUT_BRANCH}),
        )
        decision = derive_state(snap, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("checked-out branch evidence", decision.reasons)

    def test_draft_never_approves_even_with_everything_else_valid(self):
        snap = replace(
            approved_snapshot(),
            pr_is_draft=True,
            worktree_clean=True,
            ci_state=CiState.PASSING,
        )
        decision = derive_state(snap, NOW)
        self.assertNotEqual(ClosureState.APPROVED, decision.state)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)

    def test_malformed_branch_types_raise(self):
        for field in ("head_branch", "base_branch", "checked_out_branch", "pr_state"):
            with self.subTest(field=field):
                snap = replace(review_ready(), **{field: 42})
                with self.assertRaises(TypeError):
                    derive_state(snap, NOW)

    def test_malformed_pr_is_draft_type_raises(self):
        snap = replace(review_ready(), pr_is_draft="yes")
        with self.assertRaises(TypeError):
            derive_state(snap, NOW)


if __name__ == "__main__":
    unittest.main()
