"""Task 8B2 - review-record import guards and central-claim guard fixtures.

Offline, sanitized regressions for the review import authority
(``pr_closure.review.validate_review``): only a verified two-axis review
record can import as an approval, and the mandatory central-claim guard
promotes undecidable claims to a blocking disposition instead of deferring
them to the follow-up path.
"""

import unittest

from pr_closure.model import Disposition, Verdict
from pr_closure.review import ReviewValidationError, validate_review


def _verified_follow_up_record():
    """Cross-family, two-axis review record with one filed follow-up issue."""
    return {
        "schema_version": 1,
        "repository": "owner/repo",
        "pr_number": 42,
        "reviewed_branch": "feat/pr-closure-framework",
        "reviewed_commit": "a" * 40,
        "base_commit": "b" * 40,
        "implementer_family": "deepseek",
        "reviewer_family": "gpt",
        "local_evidence": ["tests:tools/tests/test_review_guards.py"],
        "ci_evidence": ["ci:pr-check/run-1"],
        "verdict": "APPROVED_WITH_FOLLOW_UPS",
        "findings": [{
            "id": "F-1",
            "root_cause": "missing-output-test",
            "severity": "MINOR",
            "disposition": "FOLLOW_UP_ISSUE",
            "scope": "pre_existing",
            "summary": "Operator output lacks a mutation guard",
            "evidence": ["476 tests survive the mutation"],
            "follow_up_issue": 900,
        }],
        "intentionally_not_findings": [],
    }


class ReviewImportGuardTests(unittest.TestCase):
    """Only a structured two-axis review record can import as an approval."""

    def test_free_form_one_line_verdict_cannot_import_as_approval(self):
        record = _verified_follow_up_record()
        record["verdict"] = "Approved. LGTM, ship it."
        with self.assertRaisesRegex(ReviewValidationError, "verdict"):
            validate_review(record)

    def test_milestone_only_approval_cannot_import_as_approval(self):
        # A milestone/Trello card is not a two-axis review record: no verdict
        # axis and no findings axis, so the import gate rejects it closed.
        milestone_card = {
            "milestone": "closure-approved",
            "labels": ["approved"],
            "pr_number": 42,
        }
        with self.assertRaisesRegex(ReviewValidationError, "milestone"):
            validate_review(milestone_card)

    def test_malformed_two_axis_review_record_cannot_import_as_approval(self):
        for missing in ("verdict", "findings"):
            with self.subTest(missing_axis=missing):
                record = _verified_follow_up_record()
                del record[missing]
                with self.assertRaisesRegex(ReviewValidationError, missing):
                    validate_review(record)

    def test_verified_follow_up_record_remains_eligible_for_follow_up_path(self):
        review = validate_review(_verified_follow_up_record())
        self.assertEqual(Verdict.APPROVED_WITH_FOLLOW_UPS, review.verdict)
        self.assertEqual(Disposition.FOLLOW_UP_ISSUE, review.findings[0].disposition)
        self.assertEqual(900, review.findings[0].follow_up_issue)
        # Not promoted: the follow-up path owns the finding, not the blocker path.
        self.assertNotIn(
            Disposition.BLOCKS_PR, [finding.disposition for finding in review.findings]
        )


class CentralClaimGuardTests(unittest.TestCase):
    """The central-claim guard cannot classify an undecidable claim as safe."""

    def _undecidable_central_claim_record(self):
        record = _verified_follow_up_record()
        record["findings"][0].update({
            "severity": "MEDIUM",
            "disposition": "FOLLOW_UP_ISSUE",
            "scope": "central_claim",
            "summary": "Central claim cannot be decided from the supplied evidence",
        })
        return record

    def test_undecidable_central_claim_is_promoted_to_blocking_disposition(self):
        review = validate_review(self._undecidable_central_claim_record())
        self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)
        self.assertEqual(Verdict.CHANGES_REQUIRED, review.verdict)

    def test_follow_up_path_cannot_defer_an_undecidable_central_claim(self):
        # A filed follow-up issue is not enough: the guard promotes the claim to
        # BLOCKS_PR, so no FOLLOW_UP_ISSUE survives and APPROVED_WITH_FOLLOW_UPS
        # is impossible.
        review = validate_review(self._undecidable_central_claim_record())
        dispositions = [finding.disposition for finding in review.findings]
        self.assertNotIn(Disposition.FOLLOW_UP_ISSUE, dispositions)
        self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)
        self.assertEqual(Verdict.CHANGES_REQUIRED, review.verdict)


if __name__ == "__main__":
    unittest.main()
