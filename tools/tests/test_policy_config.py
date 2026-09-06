import copy
import unittest

from jsonschema import Draft202012Validator

from pr_closure.contract import (
    ConfigValidationError,
    project_json_schema,
    validate_project_config,
)
from pr_closure.model import ReviewPolicyMode


def valid_config():
    return {
        "schema_version": 1,
        "project": "publyapp",
        "repository": "owner/repo",
        "repo_path": "/var/tmp/durable/repo",
        "default_branch": "develop",
        "closure_state_dir": "/var/tmp/durable/state",
        "local_review_ready_commands": ["npm run verify"],
        "closure_acceptance_commands": ["npm run acceptance"],
        "infra_retry_budget": 1,
        "stagnation_budget_minutes": 240,
        "heavy_job_limit": 1,
        "verification_command_timeout_seconds": 300,
        "tracking_projection": None,
    }


def active_policy_config():
    config = valid_config()
    config["model_routes"] = [
        {
            "id": "publyapp-luna-to-sol-v1",
            "registry_version": "models-v1",
            "launcher_registry_version": "launchers-v1",
            "implementer_model": "gpt-5.6-luna",
            "implementer_runner": "codex",
            "implementer_invocation_model": "gpt-5.6-luna",
            "reviewer_model": "gpt-5.6-sol",
            "reviewer_runner": "codex",
            "reviewer_invocation_model": "gpt-5.6-sol",
            "same_family_policy_id": "publyapp-gpt-implementation-sol-review-v1",
        },
        {
            "id": "publyapp-deepseek-to-sol-v1",
            "registry_version": "models-v1",
            "launcher_registry_version": "launchers-v1",
            "implementer_model": "deepseek-v4-flash",
            "implementer_runner": "opencode",
            "implementer_invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
            "reviewer_model": "gpt-5.6-sol",
            "reviewer_runner": "codex",
            "reviewer_invocation_model": "gpt-5.6-sol",
            "same_family_policy_id": None,
        },
    ]
    config["review_policy"] = {
        "mode": "staged",
        "owner_authorization": "Radan; owner instruction 2026-09-05",
        "forbidden_reviewer_families": ["anthropic"],
        "same_family_exceptions": [
            {
                "id": "publyapp-gpt-implementation-sol-review-v1",
                "registry_version": "models-v1",
                "implementer_family": "openai",
                "reviewer_model": "gpt-5.6-sol",
                "required_for_authorized_family": True,
                "owner_authorization": "Radan; owner instruction 2026-09-05",
                "rationale": "GPT implementation is reviewed by gpt-5.6-sol; Claude is forbidden.",
            }
        ],
    }
    return config


class PolicyConfigTests(unittest.TestCase):
    def test_generated_schema_accepts_disabled_and_active_policy_shapes(self):
        validator = Draft202012Validator(project_json_schema())
        self.assertTrue(validator.is_valid(valid_config()))
        self.assertTrue(validator.is_valid(active_policy_config()))

    def test_generated_schema_rejects_structurally_invalid_policy_shapes(self):
        validator = Draft202012Validator(project_json_schema())
        cases = []
        unknown = active_policy_config()
        unknown["review_policy"]["surprise"] = True
        cases.append(unknown)
        no_routes = active_policy_config()
        no_routes["model_routes"] = []
        cases.append(no_routes)
        bad_mode = active_policy_config()
        bad_mode["review_policy"]["mode"] = "optional"
        cases.append(bad_mode)
        bad_boolean = active_policy_config()
        bad_boolean["review_policy"]["same_family_exceptions"][0][
            "required_for_authorized_family"
        ] = "true"
        cases.append(bad_boolean)
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertFalse(validator.is_valid(raw))

    def test_absent_policy_preserves_disabled_compatibility(self):
        config = validate_project_config(valid_config())
        self.assertIsNone(config.review_policy.mode)
        self.assertEqual((), config.model_routes)

    def test_explicit_empty_policy_is_disabled(self):
        raw = valid_config()
        raw["review_policy"] = {
            "mode": None,
            "owner_authorization": None,
            "forbidden_reviewer_families": [],
            "same_family_exceptions": [],
        }
        raw["model_routes"] = []
        config = validate_project_config(raw)
        self.assertIsNone(config.review_policy.mode)

    def test_valid_publyapp_policy_parses_to_immutable_values(self):
        config = validate_project_config(active_policy_config())
        self.assertEqual(ReviewPolicyMode.STAGED, config.review_policy.mode)
        self.assertEqual("openai", config.review_policy.same_family_exceptions[0].implementer_family)
        self.assertEqual("gpt-5.6-sol", config.model_routes[0].reviewer_model)
        self.assertIsInstance(config.model_routes, tuple)

    def test_enforced_mode_is_explicitly_supported(self):
        raw = active_policy_config()
        raw["review_policy"]["mode"] = "enforced"
        config = validate_project_config(raw)
        self.assertEqual(ReviewPolicyMode.ENFORCED, config.review_policy.mode)

    def test_non_empty_policy_requires_mode_owner_and_routes(self):
        for mutation in ("mode", "owner_authorization", "model_routes"):
            raw = active_policy_config()
            if mutation == "model_routes":
                raw[mutation] = []
            else:
                raw["review_policy"].pop(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ConfigValidationError):
                    validate_project_config(raw)

    def test_disabled_policy_rejects_routes_or_content(self):
        raw = valid_config()
        raw["model_routes"] = active_policy_config()["model_routes"]
        with self.assertRaises(ConfigValidationError):
            validate_project_config(raw)

        raw = valid_config()
        raw["review_policy"] = {
            "mode": None,
            "owner_authorization": None,
            "forbidden_reviewer_families": ["anthropic"],
            "same_family_exceptions": [],
        }
        with self.assertRaises(ConfigValidationError):
            validate_project_config(raw)

    def test_unknown_policy_route_and_exception_keys_are_rejected(self):
        for location in ("policy", "route", "exception"):
            raw = active_policy_config()
            if location == "policy":
                raw["review_policy"]["surprise"] = True
            elif location == "route":
                raw["model_routes"][0]["surprise"] = True
            else:
                raw["review_policy"]["same_family_exceptions"][0]["surprise"] = True
            with self.subTest(location=location):
                with self.assertRaises(ConfigValidationError):
                    validate_project_config(raw)

    def test_routes_and_exceptions_are_unique(self):
        for mutation in ("route_id", "implementer_model", "exception_id", "forbidden_family"):
            raw = active_policy_config()
            if mutation == "route_id":
                duplicate = copy.deepcopy(raw["model_routes"][1])
                duplicate["id"] = raw["model_routes"][0]["id"]
                raw["model_routes"].append(duplicate)
            elif mutation == "implementer_model":
                duplicate = copy.deepcopy(raw["model_routes"][0])
                duplicate["id"] = "another-route"
                raw["model_routes"].append(duplicate)
            elif mutation == "exception_id":
                raw["review_policy"]["same_family_exceptions"].append(
                    copy.deepcopy(raw["review_policy"]["same_family_exceptions"][0])
                )
            else:
                raw["review_policy"]["forbidden_reviewer_families"].append("anthropic")
            with self.subTest(mutation=mutation):
                with self.assertRaises(ConfigValidationError):
                    validate_project_config(raw)

    def test_every_route_must_match_exact_registry_and_launcher_entries(self):
        for field, value in (
            ("implementer_model", "GPT-5.6-LUNA"),
            ("implementer_runner", "jcode"),
            ("implementer_invocation_model", "luna"),
            ("reviewer_model", "gpt-5.6-terra"),
            ("reviewer_runner", "jcode"),
            ("launcher_registry_version", "launchers-v2"),
        ):
            raw = active_policy_config()
            raw["model_routes"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ConfigValidationError):
                    validate_project_config(raw)

    def test_policy_cannot_authorize_self_review_or_forbidden_reviewer(self):
        raw = active_policy_config()
        raw["model_routes"][0]["reviewer_model"] = "gpt-5.6-luna"
        raw["model_routes"][0]["reviewer_invocation_model"] = "gpt-5.6-luna"
        with self.assertRaises(ConfigValidationError):
            validate_project_config(raw)

        raw = active_policy_config()
        exception = raw["review_policy"]["same_family_exceptions"][0]
        exception["reviewer_model"] = "claude-sonnet-5"
        with self.assertRaises(ConfigValidationError):
            validate_project_config(raw)

    def test_required_family_exception_applies_to_every_authorized_model(self):
        raw = active_policy_config()
        terra = copy.deepcopy(raw["model_routes"][0])
        terra["id"] = "publyapp-terra-to-sol-v1"
        terra["implementer_model"] = "gpt-5.6-terra"
        terra["implementer_invocation_model"] = "gpt-5.6-terra"
        raw["model_routes"].append(terra)
        with self.assertRaises(ConfigValidationError):
            validate_project_config(raw)

    def test_cross_family_route_cannot_claim_same_family_exception(self):
        raw = active_policy_config()
        raw["model_routes"][1]["same_family_policy_id"] = (
            "publyapp-gpt-implementation-sol-review-v1"
        )
        with self.assertRaises(ConfigValidationError):
            validate_project_config(raw)


if __name__ == "__main__":
    unittest.main()
