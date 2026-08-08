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
        worktree_clean=True,
        local_verification=True,
        ci_state=CiState.PASSING,
        review_owned=False,
        review_verdict=None,
        review_commit=None,
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


if __name__ == "__main__":
    unittest.main()
