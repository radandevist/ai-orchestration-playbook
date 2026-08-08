import json
import unittest
from pathlib import Path

from pr_closure.model import Disposition, Severity, Verdict
from pr_closure.review import ReviewValidationError, validate_review

try:
    from jsonschema import Draft202012Validator

    HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover - environment without jsonschema
    HAS_JSONSCHEMA = False


class ReviewValidationTests(unittest.TestCase):
    def valid_record(self):
        return {
            "schema_version": 1,
            "repository": "owner/repo",
            "pr_number": 42,
            "reviewed_branch": "feat/pr-closure-framework",
            "reviewed_commit": "a" * 40,
            "base_commit": "b" * 40,
            "implementer_family": "deepseek",
            "reviewer_family": "claude",
            "local_evidence": ["tests:tools/tests/test_review.py"],
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

    def test_rejects_follow_up_issue_outside_follow_up_disposition(self):
        record = self.valid_record()
        record["findings"][0]["disposition"] = "NOTE_ONLY"
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

    def test_rejects_commit_id_with_trailing_newline(self):
        record = self.valid_record()
        record["reviewed_commit"] = "a" * 40 + "\n"
        with self.assertRaisesRegex(ReviewValidationError, "reviewed_commit"):
            validate_review(record)

    def test_rejects_base_commit_with_trailing_newline(self):
        record = self.valid_record()
        record["base_commit"] = "a" * 40 + "\n"
        with self.assertRaisesRegex(ReviewValidationError, "base_commit"):
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

    def test_gpt_and_o_series_reasoning_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "GPT-5"
        record["reviewer_family"] = "o3"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_claude_and_anthropic_prefix_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "Claude Opus 5"
        record["reviewer_family"] = "anthropic/claude-sonnet-5"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_gemini_variants_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "gemini-3-pro"
        record["reviewer_family"] = "google/gemini-2.5-flash"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_grok_variants_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "grok-4"
        record["reviewer_family"] = "xai/grok-3-mini"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_glm_and_zhipu_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "zhipu/glm-4.5"
        record["reviewer_family"] = "glm-4"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_qwen_and_alibaba_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "alibaba/qwen2.5-max"
        record["reviewer_family"] = "qwen3"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_kimi_and_moonshot_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "moonshot/kimi-k2"
        record["reviewer_family"] = "kimi"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_minimax_is_a_family(self):
        record = self.valid_record()
        record["implementer_family"] = "minimax"
        record["reviewer_family"] = "abab-6.5"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_mimo_and_xiaomi_are_same_family(self):
        record = self.valid_record()
        record["implementer_family"] = "xiaomi/mimo"
        record["reviewer_family"] = "MiMo"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_rejects_unknown_family_strings(self):
        for unknown in ("gptzero", "claude-killer", "wizard-9000", "mario"):
            record = self.valid_record()
            record["implementer_family"] = unknown
            with self.subTest(unknown=unknown):
                with self.assertRaisesRegex(ReviewValidationError, "model family"):
                    validate_review(record)

    def test_cross_family_still_accepted(self):
        record = self.valid_record()
        record["implementer_family"] = "deepseek-r1"
        record["reviewer_family"] = "anthropic/claude-opus"
        review = validate_review(record)
        self.assertEqual("deepseek", review.implementer_family)
        self.assertEqual("anthropic", review.reviewer_family)

    def test_mandatory_scope_is_case_insensitive(self):
        record = self.valid_record()
        record["verdict"] = "APPROVED"
        record["findings"][0].update({
            "severity": "CRITICAL",
            "disposition": "NOTE_ONLY",
            "scope": "Security",
            "summary": "Authorization uncertainty in the new guard",
        })
        del record["findings"][0]["follow_up_issue"]
        review = validate_review(record)
        self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)
        self.assertEqual(Verdict.CHANGES_REQUIRED, review.verdict)

    def test_mandatory_scope_is_whitespace_tolerant(self):
        record = self.valid_record()
        record["verdict"] = "APPROVED"
        record["findings"][0].update({
            "severity": "CRITICAL",
            "disposition": "NOTE_ONLY",
            "scope": "  ci  ",
            "summary": "Branch CI is red",
        })
        del record["findings"][0]["follow_up_issue"]
        review = validate_review(record)
        self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)
        self.assertEqual(Verdict.CHANGES_REQUIRED, review.verdict)

    def test_every_mandatory_scope_blocks_in_a_noncanonical_spelling(self):
        noncanonical = {
            "central_claim": "Central_Claim",
            "acceptance": "  acceptance ",
            "regression": "Regression",
            "security": "Security",
            "privacy": " PRIVACY ",
            "authorization": "Authorization",
            "billing": "Billing",
            "data_integrity": "Data_Integrity",
            "ci": "CI",
            "verification": "  verification ",
            "review_tip": "Review_Tip",
        }
        for scope, spelling in noncanonical.items():
            with self.subTest(scope=scope):
                record = self.valid_record()
                record["verdict"] = "APPROVED"
                record["findings"][0].update({
                    "severity": "NOTE",
                    "disposition": "NOTE_ONLY",
                    "scope": spelling,
                })
                del record["findings"][0]["follow_up_issue"]
                review = validate_review(record)
                self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)
                self.assertEqual(Verdict.CHANGES_REQUIRED, review.verdict)

    def test_scope_is_normalized_when_stored(self):
        record = self.valid_record()
        record["findings"][0]["scope"] = "  Data_Integrity  "
        review = validate_review(record)
        self.assertEqual("data_integrity", review.findings[0].scope)
        self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)

    def test_non_string_or_empty_scope_is_rejected(self):
        for bad_scope in ("", "   ", [], {}, None):
            record = self.valid_record()
            record["findings"][0]["scope"] = bad_scope
            with self.subTest(scope=bad_scope):
                with self.assertRaisesRegex(ReviewValidationError, "scope"):
                    validate_review(record)

    def test_rejects_unknown_top_level_key(self):
        record = self.valid_record()
        record["bonus_evidence"] = "nope"
        with self.assertRaisesRegex(ReviewValidationError, "bonus_evidence"):
            validate_review(record)

    def test_rejects_unknown_finding_key(self):
        record = self.valid_record()
        record["findings"][0]["lol_ignore"] = "nope"
        with self.assertRaisesRegex(ReviewValidationError, "lol_ignore"):
            validate_review(record)

    def test_rejects_null_or_empty_root_cause(self):
        for bad in (None, "", "   "):
            record = self.valid_record()
            record["findings"][0]["root_cause"] = bad
            with self.subTest(root_cause=bad):
                with self.assertRaisesRegex(ReviewValidationError, "root_cause"):
                    validate_review(record)

    def test_rejects_non_string_summary(self):
        record = self.valid_record()
        record["findings"][0]["summary"] = 123
        with self.assertRaisesRegex(ReviewValidationError, "summary"):
            validate_review(record)

    def test_rejects_empty_summary(self):
        record = self.valid_record()
        record["findings"][0]["summary"] = ""
        with self.assertRaisesRegex(ReviewValidationError, "summary"):
            validate_review(record)

    def test_rejects_repository_with_invalid_segments(self):
        for bad in ("a/b/c", "/x", "x/", "/", "noslash"):
            record = self.valid_record()
            record["repository"] = bad
            with self.subTest(repository=bad):
                with self.assertRaisesRegex(ReviewValidationError, "repository"):
                    validate_review(record)

    def test_rejects_empty_evidence_entry(self):
        record = self.valid_record()
        record["findings"][0]["evidence"] = ["ok", "", "   "]
        with self.assertRaisesRegex(ReviewValidationError, "evidence"):
            validate_review(record)

    def test_rejects_empty_intentionally_not_finding(self):
        record = self.valid_record()
        record["intentionally_not_findings"] = ["fine", ""]
        with self.assertRaisesRegex(ReviewValidationError, "intentionally_not_findings"):
            validate_review(record)

    def test_malformed_enum_values_raise_review_validation_error(self):
        cases = (
            (("severity", ["CRITICAL"]), "severity"),
            (("verdict", {"a": 1}), "verdict"),
            (("findings", "not-a-list"), "findings"),
            (("implementer_family", ["deepseek"]), "implementer_family"),
        )
        for (key, value), pattern in cases:
            record = self.valid_record()
            record[key] = value
            with self.subTest(key=key):
                with self.assertRaisesRegex(ReviewValidationError, pattern):
                    validate_review(record)

    def test_findings_evidence_and_not_findings_are_tuples(self):
        review = validate_review(self.valid_record())
        self.assertIsInstance(review.findings, tuple)
        self.assertIsInstance(review.findings[0].evidence, tuple)
        self.assertIsInstance(review.intentionally_not_findings, tuple)
        self.assertIsInstance(review.local_evidence, tuple)
        self.assertIsInstance(review.ci_evidence, tuple)

    def test_validated_record_is_deeply_immutable(self):
        review = validate_review(self.valid_record())
        with self.assertRaises(AttributeError):
            review.findings.append("garbage")
        with self.assertRaises(AttributeError):
            review.findings[0].evidence.append("fabricated")
        with self.assertRaises(AttributeError):
            review.intentionally_not_findings.append("x")
        with self.assertRaises(AttributeError):
            review.local_evidence.append("x")
        with self.assertRaises(AttributeError):
            review.ci_evidence.append("x")

    def test_promotion_does_not_alias_input_lists(self):
        record = self.valid_record()
        record["verdict"] = "APPROVED"
        record["findings"][0].update({
            "severity": "CRITICAL",
            "disposition": "NOTE_ONLY",
            "scope": "security",
        })
        del record["findings"][0]["follow_up_issue"]
        caller_evidence = record["findings"][0]["evidence"]
        review = validate_review(record)
        caller_evidence.append("fabricated after validation")
        self.assertEqual(("476 tests survive the mutation",), review.findings[0].evidence)
        self.assertIsInstance(review.findings[0].evidence, tuple)

    def test_rejects_missing_reviewed_branch(self):
        record = self.valid_record()
        del record["reviewed_branch"]
        with self.assertRaisesRegex(ReviewValidationError, "reviewed_branch"):
            validate_review(record)

    def test_rejects_missing_both_base_commit_and_comparison_range(self):
        record = self.valid_record()
        del record["base_commit"]
        with self.assertRaisesRegex(ReviewValidationError, "base_commit"):
            validate_review(record)

    def test_rejects_both_base_commit_and_comparison_range(self):
        record = self.valid_record()
        record["comparison_range"] = "c236e90..5dcfc09"
        with self.assertRaisesRegex(ReviewValidationError, "comparison_range"):
            validate_review(record)

    def test_accepts_comparison_range_instead_of_base_commit(self):
        record = self.valid_record()
        del record["base_commit"]
        record["comparison_range"] = "c236e90..5dcfc09"
        review = validate_review(record)
        self.assertEqual("c236e90..5dcfc09", review.comparison_range)
        self.assertIsNone(review.base_commit)

    def test_rejects_invalid_base_commit(self):
        for bad in ("abc", "A" * 40, "z" * 40):
            record = self.valid_record()
            record["base_commit"] = bad
            with self.subTest(base_commit=bad):
                with self.assertRaisesRegex(ReviewValidationError, "base_commit"):
                    validate_review(record)

    def test_rejects_empty_comparison_range(self):
        record = self.valid_record()
        del record["base_commit"]
        record["comparison_range"] = ""
        with self.assertRaisesRegex(ReviewValidationError, "comparison_range"):
            validate_review(record)

    def test_requires_local_evidence(self):
        record = self.valid_record()
        record["local_evidence"] = []
        with self.assertRaisesRegex(ReviewValidationError, "local_evidence"):
            validate_review(record)

    def test_requires_ci_evidence(self):
        record = self.valid_record()
        record["ci_evidence"] = []
        with self.assertRaisesRegex(ReviewValidationError, "ci_evidence"):
            validate_review(record)

    def test_rejects_empty_evidence_reference_entry(self):
        record = self.valid_record()
        record["ci_evidence"] = ["ci:run-1", ""]
        with self.assertRaisesRegex(ReviewValidationError, "ci_evidence"):
            validate_review(record)

    def test_bad_case_evidence_requires_good_case(self):
        record = self.valid_record()
        record["findings"][0]["bad_case_evidence"] = ["escape.sh"]
        with self.assertRaisesRegex(ReviewValidationError, "together"):
            validate_review(record)

    def test_good_case_evidence_requires_bad_case(self):
        record = self.valid_record()
        record["findings"][0]["good_case_evidence"] = ["control.sh"]
        with self.assertRaisesRegex(ReviewValidationError, "together"):
            validate_review(record)

    def test_paired_evidence_accepted_when_together(self):
        record = self.valid_record()
        record["findings"][0]["bad_case_evidence"] = ["escape.sh"]
        record["findings"][0]["good_case_evidence"] = ["control.sh"]
        review = validate_review(record)
        self.assertEqual(("escape.sh",), review.findings[0].bad_case_evidence)
        self.assertEqual(("control.sh",), review.findings[0].good_case_evidence)

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


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is not installed")
class SchemaAgreementTests(unittest.TestCase):
    def schema(self):
        path = Path(__file__).resolve().parent.parent / "schemas" / "review-record-v1.json"
        return json.loads(path.read_text())

    def valid_record(self):
        return {
            "schema_version": 1,
            "repository": "owner/repo",
            "pr_number": 42,
            "reviewed_branch": "feat/pr-closure-framework",
            "reviewed_commit": "a" * 40,
            "base_commit": "b" * 40,
            "implementer_family": "deepseek",
            "reviewer_family": "claude",
            "local_evidence": ["tests:tools/tests/test_review.py"],
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

    def schema_valid(self, record):
        return Draft202012Validator(self.schema()).is_valid(record)

    def _mutations(self):
        return {
            "unknown top-level key": dict(self.valid_record(), rogue="x"),
            "unknown finding key": (
                lambda r: dict(r, findings=[dict(r["findings"][0], rogue="x")])
            )(self.valid_record()),
            "null root_cause": (
                lambda r: dict(r, findings=[dict(r["findings"][0], root_cause=None)])
            )(self.valid_record()),
            "empty scope": (
                lambda r: dict(r, findings=[dict(r["findings"][0], scope="")])
            )(self.valid_record()),
            "int summary": (
                lambda r: dict(r, findings=[dict(r["findings"][0], summary=123)])
            )(self.valid_record()),
            "repository a/b/c": dict(self.valid_record(), repository="a/b/c"),
            "unknown verdict": dict(self.valid_record(), verdict="LOOKS_FINE"),
            "bad commit": dict(self.valid_record(), reviewed_commit="NOPE"),
            "follow_up_issue on NOTE_ONLY": (
                lambda r: dict(r, findings=[dict(r["findings"][0], disposition="NOTE_ONLY")])
            )(self.valid_record()),
            "both base_commit and comparison_range": dict(
                self.valid_record(), comparison_range="c236e90..5dcfc09"
            ),
            "neither base_commit nor comparison_range": dict(
                self.valid_record(), base_commit=None
            ),
            "empty local_evidence": dict(self.valid_record(), local_evidence=[]),
            "empty ci_evidence": dict(self.valid_record(), ci_evidence=[]),
        }

    def test_python_and_schema_agree_on_structural_acceptance(self):
        valid = self.valid_record()
        self.assertTrue(self.schema_valid(valid))
        validate_review(valid)
        for label, record in self._mutations().items():
            with self.subTest(label=label):
                schema_ok = self.schema_valid(record)
                try:
                    validate_review(record)
                    python_ok = True
                except ReviewValidationError:
                    python_ok = False
                self.assertEqual(
                    schema_ok,
                    python_ok,
                    f"agreement broken for {label}: schema={schema_ok} python={python_ok}",
                )
                self.assertFalse(schema_ok, f"expected both validators to reject {label}")

    def test_duplicate_finding_ids_are_python_only_enforcement(self):
        record = self.valid_record()
        record["findings"].append(dict(record["findings"][0], severity="MAJOR"))
        self.assertTrue(self.schema_valid(record))
        with self.assertRaisesRegex(ReviewValidationError, "duplicate"):
            validate_review(record)

    def test_schema_documents_duplicate_id_limitation_in_comment(self):
        comment = self.schema().get("$comment", "")
        self.assertIn("duplicate", comment.casefold())
        self.assertNotIn("uniqueIDs", self.schema())


if __name__ == "__main__":
    unittest.main()
