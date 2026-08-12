import unittest
from pathlib import Path

from pr_closure.model import Disposition, Severity, Verdict
from pr_closure.review import ReviewValidationError, normalize_family, validate_review


def _valid_record():
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


class ConfiguredFamilySpellingTests(unittest.TestCase):
    CONFIGURED = {
        "deepseek-v4-flash": "deepseek",
        "cline-pass/cline-pass/deepseek-v4-flash": "deepseek",
        "opencode/deepseek-v4-flash-free": "deepseek",
        "gpt-5.3-codex": "openai",
        "gpt-5.3-codex-spark": "openai",
        "gpt-5.6-sol": "openai",
        "gpt-5.6-luna": "openai",
        "claude-opus-5": "anthropic",
        "anthropic/claude-sonnet-5": "anthropic",
        "openrouter/anthropic/claude-opus-5": "anthropic",
        "kimi-k2": "moonshot",
        "kimi-k2.6": "moonshot",
        "mimo-v2.5-pro": "xiaomi",
        "qwen3.7-max": "alibaba",
        "glm-5.2": "zhipu",
        "z-ai/glm-5.2": "zhipu",
        "x-ai/grok-4": "xai",
        "Claude Opus 5": "anthropic",
    }

    UNKNOWN = ("gptzero", "claude-killer", "wizard-9000", "mario", "x", "x5")

    CROSS_LINEAGE_PAIRS = (
        ("deepseek-v4-flash", "gpt-5.3-codex"),
        ("opencode/deepseek-v4-flash-free", "claude-opus-5"),
        ("gpt-5.6-sol", "kimi-k2.6"),
        ("claude-opus-5", "mimo-v2.5-pro"),
        ("kimi-k2.6", "qwen3.7-max"),
        ("mimo-v2.5-pro", "glm-5.2"),
        ("qwen3.7-max", "x-ai/grok-4"),
        ("glm-5.2", "deepseek-v4-flash"),
        ("x-ai/grok-4", "minimax"),
    )

    SAME_LINEAGE_PAIRS = (
        ("openrouter/anthropic/claude-opus-5", "claude-opus-5"),
        ("gpt-5.6-sol", "gpt-5.3-codex-spark"),
        ("cline-pass/cline-pass/deepseek-v4-flash", "opencode/deepseek-v4-flash-free"),
        ("kimi-k2.6", "moonshot/kimi-k2"),
        ("z-ai/glm-5.2", "glm-4"),
        ("x-ai/grok-4", "grok-3-mini"),
    )

    def test_every_configured_spelling_resolves(self):
        failures = []
        for spelling, expected in self.CONFIGURED.items():
            try:
                got = normalize_family(spelling)
            except ReviewValidationError as error:
                failures.append(f"{spelling} -> {error}")
                continue
            if got != expected:
                failures.append(f"{spelling!r} -> {got!r}")
        self.assertEqual([], failures)

    def test_unknown_and_lookalike_spellings_are_still_rejected(self):
        failures = []
        for spelling in self.UNKNOWN:
            try:
                got = normalize_family(spelling)
                failures.append(f"{spelling!r} -> {got!r}")
            except ReviewValidationError:
                pass
        self.assertEqual([], failures)

    def test_configured_cross_lineage_pairs_are_recordable(self):
        failures = []
        for implementer, reviewer in self.CROSS_LINEAGE_PAIRS:
            record = _valid_record()
            record["implementer_family"] = implementer
            record["reviewer_family"] = reviewer
            try:
                validate_review(record)
            except ReviewValidationError as error:
                failures.append(f"{implementer} vs {reviewer} unrecordable: {error}")
        self.assertEqual([], failures)

    def test_same_lineage_pairs_are_still_rejected(self):
        for first, second in self.SAME_LINEAGE_PAIRS:
            with self.subTest(first=first, second=second):
                record = _valid_record()
                record["implementer_family"] = first
                record["reviewer_family"] = second
                with self.assertRaisesRegex(
                    ReviewValidationError, "different model family"
                ):
                    validate_review(record)

    def test_namespace_that_conflicts_with_head_is_ambiguous(self):
        for spelling in ("anthropic/gpt-5", "openrouter/anthropic/gpt-5"):
            with self.subTest(spelling=spelling):
                with self.assertRaisesRegex(ReviewValidationError, "ambiguous"):
                    normalize_family(spelling)

    def test_conflicting_namespaces_are_ambiguous(self):
        for spelling in (
            "anthropic/openai/wizard-9000",
            "xai/anthropic/wizard-9000",
        ):
            with self.subTest(spelling=spelling):
                with self.assertRaisesRegex(ReviewValidationError, "ambiguous"):
                    normalize_family(spelling)

    def test_namespace_fallback_saves_an_unresolvable_head(self):
        results = []
        for spelling in (
            "anthropic/wizard-9000",
            "openrouter/anthropic/wizard-9000",
        ):
            try:
                results.append(normalize_family(spelling))
            except ReviewValidationError as error:
                results.append(f"rejected: {error}")
        self.assertEqual(["anthropic", "anthropic"], results)


class GeneratedSchemaTests(unittest.TestCase):
    def published_schema(self):
        path = Path(__file__).resolve().parent.parent / "schemas" / "review-record-v1.json"
        return path.read_text()

    def test_published_schema_is_exactly_what_the_gate_generates(self):
        from pr_closure.contract import render_schema

        published = self.published_schema()
        generated = render_schema()
        self.assertEqual(
            generated,
            published,
            "tools/schemas/review-record-v1.json drifted from contract.py - regenerate "
            "with PYTHONPATH=tools python3 -m pr_closure.contract",
        )

if __name__ == "__main__":
    unittest.main()
