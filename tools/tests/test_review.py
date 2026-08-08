import unittest

from pr_closure.model import Disposition, Severity, Verdict
from pr_closure.review import ReviewValidationError, validate_review


class ReviewValidationTests(unittest.TestCase):
    def valid_record(self):
        return {
            "schema_version": 1,
            "repository": "owner/repo",
            "pr_number": 42,
            "reviewed_commit": "a" * 40,
            "implementer_family": "deepseek",
            "reviewer_family": "claude",
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

    def test_accepts_cross_family_approval_with_filed_follow_up(self):
        review = validate_review(self.valid_record())
        self.assertEqual(Verdict.APPROVED_WITH_FOLLOW_UPS, review.verdict)
        self.assertEqual(Disposition.FOLLOW_UP_ISSUE, review.findings[0].disposition)

    def test_rejects_same_family_review(self):
        record = self.valid_record()
        record["reviewer_family"] = "deepseek"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_rejects_follow_up_without_issue(self):
        record = self.valid_record()
        del record["findings"][0]["follow_up_issue"]
        with self.assertRaisesRegex(ReviewValidationError, "follow_up_issue"):
            validate_review(record)

    def test_promotes_central_claim_failure_to_blocker(self):
        record = self.valid_record()
        record["verdict"] = "APPROVED_WITH_FOLLOW_UPS"
        record["findings"][0].update({
            "severity": "MEDIUM",
            "disposition": "FOLLOW_UP_ISSUE",
            "scope": "central_claim",
        })
        review = validate_review(record)
        self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)
        self.assertEqual(Verdict.CHANGES_REQUIRED, review.verdict)

    def test_rejects_unknown_verdict(self):
        record = self.valid_record()
        record["verdict"] = "LOOKS_FINE"
        with self.assertRaisesRegex(ReviewValidationError, "verdict"):
            validate_review(record)

    def test_rejects_missing_required_field(self):
        record = self.valid_record()
        del record["pr_number"]
        with self.assertRaisesRegex(ReviewValidationError, "pr_number"):
            validate_review(record)

    def test_rejects_malformed_commit_id(self):
        record = self.valid_record()
        record["reviewed_commit"] = "NOT_A_COMMIT"
        with self.assertRaisesRegex(ReviewValidationError, "reviewed_commit"):
            validate_review(record)

    def test_rejects_uppercase_commit_id(self):
        record = self.valid_record()
        record["reviewed_commit"] = "A" * 40
        with self.assertRaisesRegex(ReviewValidationError, "reviewed_commit"):
            validate_review(record)

    def test_rejects_duplicate_finding_ids(self):
        record = self.valid_record()
        record["findings"].append(dict(record["findings"][0], severity="MAJOR"))
        with self.assertRaisesRegex(ReviewValidationError, "duplicate"):
            validate_review(record)

    def test_rejects_unknown_severity(self):
        record = self.valid_record()
        record["findings"][0]["severity"] = "LEGENDARY"
        with self.assertRaisesRegex(ReviewValidationError, "severity"):
            validate_review(record)

    def test_rejects_unknown_disposition(self):
        record = self.valid_record()
        record["findings"][0]["disposition"] = "MAYBE_LATER"
        with self.assertRaisesRegex(ReviewValidationError, "disposition"):
            validate_review(record)

    def test_rejects_non_positive_follow_up_issue(self):
        record = self.valid_record()
        record["findings"][0]["follow_up_issue"] = 0
        with self.assertRaisesRegex(ReviewValidationError, "follow_up_issue"):
            validate_review(record)

    def test_requires_at_least_one_follow_up_for_approved_with_follow_ups(self):
        record = self.valid_record()
        record["findings"][0]["disposition"] = "NOTE_ONLY"
        del record["findings"][0]["follow_up_issue"]
        with self.assertRaisesRegex(ReviewValidationError, "follow-up"):
            validate_review(record)

    def test_same_family_by_lineage_case_insensitive(self):
        record = self.valid_record()
        record["implementer_family"] = "DeepSeek-V3.2"
        record["reviewer_family"] = "deepseek-r1"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_gpt_variants_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "gpt-4o"
        record["reviewer_family"] = "gpt-5"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_approval_after_promoted_blocker_is_forbidden(self):
        record = self.valid_record()
        record["verdict"] = "APPROVED"
        record["findings"][0].update({
            "severity": "CRITICAL",
            "disposition": "NOTE_ONLY",
            "scope": "security",
            "summary": "Authorization uncertainty in the new guard",
        })
        del record["findings"][0]["follow_up_issue"]
        review = validate_review(record)
        self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)
        self.assertEqual(Verdict.CHANGES_REQUIRED, review.verdict)

    def test_malformed_record_is_rejected_not_silently_accepted(self):
        record = self.valid_record()
        record["findings"] = "not-a-list"
        with self.assertRaisesRegex(ReviewValidationError, "findings"):
            validate_review(record)
