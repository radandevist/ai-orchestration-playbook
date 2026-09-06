import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from pr_closure.contract import validate_project_config
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

    def test_dispatch_selects_only_the_normalized_project_route_table(self):
        spec = importlib.util.find_spec("pr_closure.dispatch")
        self.assertIsNotNone(spec)
        from pr_closure.dispatch import select_model_route

        config = validate_project_config(active_policy_config())
        route = select_model_route(config, "gpt-5.6-luna")
        self.assertEqual("gpt-5.6-sol", route.reviewer_model)
        self.assertEqual("codex", route.reviewer_runner)

    def test_rollback_target_keeps_anthropic_forbidden_and_cannot_remove_policy(self):
        spec = importlib.util.find_spec("pr_closure.policy_lifecycle")
        self.assertIsNotNone(spec)
        from pr_closure.policy_lifecycle import validate_activation_projection

        staged = active_policy_config()
        rollback = active_policy_config()
        rollback["model_routes"][0].update({
            "reviewer_model": "deepseek-v4-flash",
            "reviewer_runner": "opencode",
            "reviewer_invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
            "same_family_policy_id": None,
        })
        rollback["model_routes"][1].update({
            "reviewer_model": "gpt-5.6-sol",
            "reviewer_runner": "codex",
            "reviewer_invocation_model": "gpt-5.6-sol",
            "same_family_policy_id": None,
        })
        rollback["review_policy"]["same_family_exceptions"] = []
        rollback["review_policy"]["mode"] = "enforced"
        validate_activation_projection(staged, rollback)

        rollback["review_policy"]["forbidden_reviewer_families"] = []
        with self.assertRaises(Exception):
            validate_activation_projection(staged, rollback)

        removed = {
            key: value
            for key, value in staged.items()
            if key not in ("model_routes", "review_policy")
        }
        with self.assertRaises(Exception):
            validate_activation_projection(staged, removed)


if __name__ == "__main__":
    unittest.main()
