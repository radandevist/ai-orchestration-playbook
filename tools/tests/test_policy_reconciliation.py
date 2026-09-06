import copy
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pr_closure.contract import ConfigValidationError, validate_project_config
from pr_closure.policy_lifecycle import authorize_retirement, validate_activation_projection
from pr_closure.store import RunStore
from tools.tests.test_policy_config import active_policy_config


COMMIT = "a" * 40


class PolicyReconciliationTests(unittest.TestCase):
    def test_legacy_inventory_reader_exists_and_is_advisory(self):
        spec = importlib.util.find_spec("pr_closure.migration")
        self.assertIsNotNone(spec)
        from pr_closure.migration import inventory_active_tip

        root = Path(tempfile.mkdtemp(prefix="policy-inventory-", dir="/var/tmp"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        store = RunStore(root, "project", 7)
        tip = root / "tip.json"
        tip.write_text("tip")
        store.record_commit(COMMIT, str(tip))
        store.write_review(
            COMMIT,
            "legacy",
            {
                "schema_version": 1,
                "repository": "owner/repo",
                "pr_number": 7,
                "reviewed_commit": COMMIT,
                "implementer_family": "deepseek",
                "reviewer_family": "openai",
                "verdict": "APPROVED",
            },
        )
        report = inventory_active_tip(store, COMMIT)
        self.assertEqual(1, len(report))
        self.assertFalse(report[0]["authority"])
        self.assertIn("implementer", report[0]["provenance"])
        self.assertIn("reviewer", report[0]["provenance"])
        self.assertEqual(64, len(report[0]["raw_sha256"]))
        self.assertFalse(report[0]["migration_complete"])
        self.assertEqual("replace-and-retire", report[0]["migration_decision"]["action"])
        self.assertFalse(report[0]["provenance"]["implementer"]["verified"])
        self.assertFalse(report[0]["provenance"]["reviewer"]["verified"])

    def test_legacy_inventory_resolves_aliases_and_checks_both_output_digests(self):
        from pr_closure.migration import inventory_active_tip

        root = Path(tempfile.mkdtemp(prefix="policy-inventory-output-", dir="/var/tmp"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        store = RunStore(root, "project", 7)
        tip = root / "tip.json"
        tip.write_text("tip")
        store.record_commit(COMMIT, str(tip))
        implementer_output = root / "producer" / "implementer.out"
        reviewer_output = root / "producer" / "reviewer.out"
        implementer_output.parent.mkdir()
        implementer_output.write_bytes(b"implementer output")
        reviewer_output.write_bytes(b"reviewer output")
        store.write_review(
            COMMIT,
            "legacy-with-output",
            {
                "schema_version": 1,
                "reviewed_commit": COMMIT,
                "implementer_model": "legacy-luna",
                "reviewer_model": "legacy-sol",
                "producer_outputs": {
                    "implementer": {
                        "model_id": "legacy-luna",
                        "producer_output_path": str(implementer_output),
                        "producer_output_sha256": hashlib.sha256(
                            implementer_output.read_bytes()
                        ).hexdigest(),
                    },
                    "reviewer": {
                        "model_id": "legacy-sol",
                        "producer_output_path": str(reviewer_output),
                        "producer_output_sha256": "0" * 64,
                    },
                },
            },
        )

        report = inventory_active_tip(
            store,
            COMMIT,
            legacy_aliases={
                "legacy-luna": "gpt-5.6-luna",
                "legacy-sol": "gpt-5.6-sol",
            },
        )[0]
        implementer = report["provenance"]["implementer"]
        reviewer = report["provenance"]["reviewer"]
        self.assertEqual("legacy-luna", implementer["model_id"])
        self.assertEqual("gpt-5.6-luna", implementer["canonical_model"])
        self.assertTrue(implementer["immutable_lane_output"])
        self.assertFalse(reviewer["immutable_lane_output"])
        self.assertFalse(report["migration_complete"])
        self.assertTrue(report["migration_decision"]["replacement_required"])
        self.assertTrue(report["migration_decision"]["retirement_required"])

    def test_dispatch_selects_only_the_normalized_project_route_table(self):
        spec = importlib.util.find_spec("pr_closure.dispatch")
        self.assertIsNotNone(spec)
        from pr_closure.dispatch import select_model_route

        config = validate_project_config(active_policy_config())
        route = select_model_route(config, "gpt-5.6-luna")
        self.assertEqual("gpt-5.6-sol", route.reviewer_model)
        self.assertEqual("codex", route.reviewer_runner)

    def test_production_cli_select_model_route_consumes_project_routes(self):
        root = Path(tempfile.mkdtemp(prefix="policy-dispatch-cli-", dir="/var/tmp"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        config_path = root / "config.json"
        config_path.write_text(json.dumps(active_policy_config()))
        import subprocess
        import sys

        cli = Path(__file__).resolve().parents[1] / "pr-closure"
        process = subprocess.run(
            [
                sys.executable,
                str(cli),
                "select-model-route",
                "--config",
                str(config_path),
                "--implementer-model",
                "gpt-5.6-luna",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, process.returncode, process.stderr)
        payload = json.loads(process.stdout)
        self.assertEqual("publyapp-luna-to-sol-v1", payload["id"])
        self.assertEqual("gpt-5.6-sol", payload["reviewer_model"])

    def test_exact_rollback_activation_is_mode_only_after_target_is_staged(self):
        staged = active_policy_config()
        staged["model_routes"][0].update({
            "reviewer_model": "deepseek-v4-flash",
            "reviewer_runner": "opencode",
            "reviewer_invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
            "same_family_policy_id": None,
        })
        staged["model_routes"][1]["same_family_policy_id"] = None
        staged["review_policy"]["same_family_exceptions"] = []

        projected = copy.deepcopy(staged)
        projected["review_policy"]["mode"] = "enforced"
        validate_activation_projection(staged, projected)

        old_staged = active_policy_config()
        with self.assertRaises(ConfigValidationError):
            validate_activation_projection(old_staged, projected)

    def test_rollback_retirement_contract_accepts_only_the_pinned_old_review(self):
        from pr_closure.policy_lifecycle import ROLLBACK_RETIREMENT_POLICY_ID

        root = Path(tempfile.mkdtemp(prefix="policy-rollback-", dir="/var/tmp"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        store = RunStore(root, "project", 7)
        tip = root / "tip.json"
        tip.write_text("tip")
        store.record_commit(COMMIT, str(tip))
        record = {
            "schema_version": 2,
            "repository": "owner/repo",
            "pr_number": 7,
            "reviewed_commit": COMMIT,
            "reviewed_branch": "feature/close",
            "base_commit": "b" * 40,
            "implementer_family": "openai",
            "reviewer_family": "openai",
            "implementer_model": "gpt-5.6-luna",
            "reviewer_model": "gpt-5.6-sol",
            "review_exception": {"policy_id": ROLLBACK_RETIREMENT_POLICY_ID},
            "provenance": {
                "registry_version": "models-v1",
                "launcher_registry_version": "launchers-v1",
                "implementer": {
                    "model_id": "gpt-5.6-luna",
                    "runner": "codex",
                    "invocation_model": "gpt-5.6-luna",
                    "run_ref": "orchestration://run/rollback/luna",
                    "durable_path": "/var/tmp/durable/luna-envelope.json",
                    "sha256": "0" * 64,
                },
                "reviewer": {
                    "model_id": "gpt-5.6-sol",
                    "runner": "codex",
                    "invocation_model": "gpt-5.6-sol",
                    "run_ref": "orchestration://run/rollback/sol",
                    "durable_path": "/var/tmp/durable/sol-envelope.json",
                    "sha256": "1" * 64,
                },
            },
            "local_evidence": ["tests:rollback"],
            "ci_evidence": ["ci:rollback"],
            "verdict": "APPROVED",
            "findings": [],
            "intentionally_not_findings": [],
        }
        store.write_review(COMMIT, "old-same-family", record)
        target = active_policy_config()
        target["model_routes"][0].update({
            "reviewer_model": "deepseek-v4-flash",
            "reviewer_runner": "opencode",
            "reviewer_invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
            "same_family_policy_id": None,
        })
        target["model_routes"][1]["same_family_policy_id"] = None
        target["review_policy"]["same_family_exceptions"] = []
        config = validate_project_config(target)

        authorize_retirement(
            config,
            store,
            COMMIT,
            "old-same-family",
            "policy-rollback: same-family-review-replaced",
            ROLLBACK_RETIREMENT_POLICY_ID,
        )

        bad_record = copy.deepcopy(record)
        bad_record["reviewer_model"] = "deepseek-v4-flash"
        store.write_review(COMMIT, "bad-same-family", bad_record)
        with self.assertRaises(ConfigValidationError):
            authorize_retirement(
                config,
                store,
                COMMIT,
                "bad-same-family",
                "policy-rollback: same-family-review-replaced",
                ROLLBACK_RETIREMENT_POLICY_ID,
            )


if __name__ == "__main__":
    unittest.main()
