import unittest

from jsonschema import Draft202012Validator

from pr_closure.contract import ReviewValidationError, review_json_schema_v2
from pr_closure.review import validate_review
from tools.tests.test_policy_config import active_policy_config
from pr_closure.contract import validate_project_config


def v1_record():
    return {
        "schema_version": 1,
        "repository": "owner/repo",
        "pr_number": 42,
        "reviewed_branch": "lane/change",
        "reviewed_commit": "a" * 40,
        "base_commit": "b" * 40,
        "implementer_family": "deepseek",
        "reviewer_family": "openai",
        "local_evidence": ["focused tests pass"],
        "ci_evidence": ["required CI passes"],
        "verdict": "APPROVED",
        "findings": [],
        "intentionally_not_findings": [],
    }


def v2_record():
    record = v1_record()
    record.update({
        "schema_version": 2,
        "implementer_family": "openai",
        "reviewer_family": "openai",
        "implementer_model": "gpt-5.6-luna",
        "reviewer_model": "gpt-5.6-sol",
        "review_exception": {
            "policy_id": "publyapp-gpt-implementation-sol-review-v1",
        },
        "provenance": {
            "registry_version": "models-v1",
            "launcher_registry_version": "launchers-v1",
            "implementer": {
                "model_id": "gpt-5.6-luna",
                "runner": "codex",
                "invocation_model": "gpt-5.6-luna",
                "run_ref": "orchestration://run/2026-09-05/luna-123",
                "durable_path": "/var/tmp/durable/luna-123.json",
                "sha256": "0" * 64,
            },
            "reviewer": {
                "model_id": "gpt-5.6-sol",
                "runner": "codex",
                "invocation_model": "gpt-5.6-sol",
                "run_ref": "orchestration://run/2026-09-05/sol-456",
                "durable_path": "/var/tmp/durable/sol-456.json",
                "sha256": "1" * 64,
            },
        },
    })
    return record


def active_config():
    return validate_project_config(active_policy_config())


class ReviewPolicyTests(unittest.TestCase):
    def test_generated_v2_schema_accepts_the_exact_record(self):
        validator = Draft202012Validator(review_json_schema_v2())
        self.assertTrue(validator.is_valid(v2_record()))

    def test_generated_v2_schema_rejects_partial_or_unknown_provenance(self):
        validator = Draft202012Validator(review_json_schema_v2())
        record = v2_record()
        record["provenance"]["reviewer"].pop("run_ref")
        self.assertFalse(validator.is_valid(record))
        record = v2_record()
        record["provenance"]["reviewer"]["extra"] = True
        self.assertFalse(validator.is_valid(record))

    def test_v1_compatibility_remains_cross_family_only_without_policy(self):
        self.assertEqual("APPROVED", validate_review(v1_record()).verdict.value)
        same_family = v1_record()
        same_family["implementer_family"] = "gpt-5.6-luna"
        with self.assertRaises(ReviewValidationError):
            validate_review(same_family)

    def test_active_policy_rejects_v1_before_authority(self):
        config = active_config()
        with self.assertRaisesRegex(ReviewValidationError, "schema v1"):
            validate_review(
                v1_record(),
                review_policy=config.review_policy,
                model_routes=config.model_routes,
            )

    def test_exact_luna_to_sol_route_is_authorized(self):
        config = active_config()
        review = validate_review(
            v2_record(),
            review_policy=config.review_policy,
            model_routes=config.model_routes,
        )
        self.assertEqual("gpt-5.6-luna", review.implementer_model)
        self.assertEqual("gpt-5.6-sol", review.reviewer_model)
        self.assertEqual(
            "publyapp-gpt-implementation-sol-review-v1",
            review.review_exception_id,
        )

    def test_exact_route_rejects_wrong_model_endpoint_or_exception(self):
        config = active_config()
        mutations = (
            ("reviewer_model", "gpt-5.6-luna"),
            ("implementer_model", "gpt-5.6-terra"),
            ("review_exception", {"policy_id": "other-policy"}),
        )
        for field, value in mutations:
            record = v2_record()
            record[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ReviewValidationError):
                    validate_review(
                        record,
                        review_policy=config.review_policy,
                        model_routes=config.model_routes,
                    )

    def test_cross_family_route_must_not_claim_an_exception(self):
        config = active_config()
        record = v2_record()
        record["implementer_family"] = "deepseek"
        record["implementer_model"] = "deepseek-v4-flash"
        record["provenance"]["implementer"].update({
            "model_id": "deepseek-v4-flash",
            "runner": "opencode",
            "invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
        })
        with self.assertRaises(ReviewValidationError):
            validate_review(
                record,
                review_policy=config.review_policy,
                model_routes=config.model_routes,
            )
        record.pop("review_exception")
        review = validate_review(
            record,
            review_policy=config.review_policy,
            model_routes=config.model_routes,
        )
        self.assertIsNone(review.review_exception_id)

    def test_disabled_v2_rejects_same_family_and_any_exception(self):
        with self.assertRaises(ReviewValidationError):
            validate_review(v2_record())

        record = v2_record()
        record["implementer_family"] = "deepseek"
        record["implementer_model"] = "deepseek-v4-flash"
        record["provenance"]["implementer"].update({
            "model_id": "deepseek-v4-flash",
            "runner": "opencode",
            "invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
        })
        with self.assertRaises(ReviewValidationError):
            validate_review(record)
        record.pop("review_exception")
        self.assertEqual("APPROVED", validate_review(record).verdict.value)

    def test_model_family_or_provenance_identity_mismatch_is_rejected(self):
        config = active_config()
        for mutation in ("family", "participant_model", "runner", "launcher_version"):
            record = v2_record()
            if mutation == "family":
                record["reviewer_family"] = "anthropic"
            elif mutation == "participant_model":
                record["provenance"]["reviewer"]["model_id"] = "gpt-5.6-luna"
            elif mutation == "runner":
                record["provenance"]["reviewer"]["runner"] = "jcode"
            else:
                record["provenance"]["launcher_registry_version"] = "launchers-v2"
            with self.subTest(mutation=mutation):
                with self.assertRaises(ReviewValidationError):
                    validate_review(
                        record,
                        review_policy=config.review_policy,
                        model_routes=config.model_routes,
                    )

    def test_v2_rejects_unknown_keys_and_partial_provenance(self):
        config = active_config()
        for mutation in ("unknown", "missing_reviewer", "partial_models"):
            record = v2_record()
            if mutation == "unknown":
                record["review_exception"]["extra"] = True
            elif mutation == "missing_reviewer":
                record["provenance"].pop("reviewer")
            else:
                record.pop("reviewer_model")
            with self.subTest(mutation=mutation):
                with self.assertRaises(ReviewValidationError):
                    validate_review(
                        record,
                        review_policy=config.review_policy,
                        model_routes=config.model_routes,
                    )


if __name__ == "__main__":
    unittest.main()
