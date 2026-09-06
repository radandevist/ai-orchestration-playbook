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
from tools.tests.test_review_policy import v2_record


COMMIT = "a" * 40


def _complete_legacy_record(
    root,
    store,
    *,
    pr_number=7,
    schema_version=1,
    chain_schema_version=1,
    chain_pr_number=None,
):
    tip = root / "tip.json"
    tip.write_text("tip")
    store.record_commit(COMMIT, str(tip))
    record = {
        "schema_version": schema_version,
        "repository": "owner/repo",
        "pr_number": pr_number,
        "reviewed_commit": COMMIT,
        "implementer_family": "gpt-5.6-luna",
        "reviewer_family": "gpt-5.6-sol",
        "verdict": "APPROVED",
    }
    provenance = {}
    for role, model_id in (("implementer", "gpt-5.6-luna"), ("reviewer", "gpt-5.6-sol")):
        output_path = root / "outputs" / (role + ".out")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes((role + " output").encode())
        output_digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
        run_ref = "orchestration://run/legacy/{0}".format(role)
        manifest_path = root / "runs" / "legacy" / role / "manifest.json"
        manifest = {
            "schema_version": chain_schema_version,
            "run_ref": run_ref,
            "repository": record["repository"],
            "pr_number": (
                record["pr_number"] if chain_pr_number is None else chain_pr_number
            ),
            "reviewed_commit": COMMIT,
            "registry_version": "models-v1",
            "launcher_registry_version": "launchers-v1",
            "model_id": model_id,
            "runner": "codex",
            "invocation_model": model_id,
            "producer_output_path": str(output_path),
            "producer_output_sha256": output_digest,
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        manifest_path.write_bytes(manifest_raw)
        manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
        envelope = dict(manifest)
        envelope.update({
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_digest,
        })
        envelope_path = root / "provenance" / (role + ".json")
        envelope_path.parent.mkdir(parents=True, exist_ok=True)
        envelope_raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        envelope_path.write_bytes(envelope_raw)
        provenance[role] = {
            "model_id": model_id,
            "runner": "codex",
            "invocation_model": model_id,
            "run_ref": run_ref,
            "durable_path": str(envelope_path),
            "sha256": hashlib.sha256(envelope_raw).hexdigest(),
        }
    record["provenance"] = provenance
    return record


def _complete_v2_record(root, *, repository="owner/repo", pr_number=7):
    record = v2_record()
    record.update({"repository": repository, "pr_number": pr_number})
    for role in ("implementer", "reviewer"):
        participant = record["provenance"][role]
        run_suffix = participant["run_ref"].split("orchestration://run/", 1)[1]
        output_path = root / "outputs" / (role + ".json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes((role + " producer output").encode())
        manifest = {
            "schema_version": 1,
            "run_ref": participant["run_ref"],
            "repository": record["repository"],
            "pr_number": record["pr_number"],
            "reviewed_commit": record["reviewed_commit"],
            "registry_version": record["provenance"]["registry_version"],
            "launcher_registry_version": record["provenance"][
                "launcher_registry_version"
            ],
            "model_id": participant["model_id"],
            "runner": participant["runner"],
            "invocation_model": participant["invocation_model"],
            "producer_output_path": str(output_path),
            "producer_output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        }
        manifest_path = root / "runs" / run_suffix / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        manifest_path.write_bytes(manifest_raw)
        envelope = dict(manifest)
        envelope.update({
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        })
        envelope_path = root / "provenance" / (role + ".json")
        envelope_path.parent.mkdir(parents=True, exist_ok=True)
        envelope_raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        envelope_path.write_bytes(envelope_raw)
        participant["durable_path"] = str(envelope_path)
        participant["sha256"] = hashlib.sha256(envelope_raw).hexdigest()
    return record


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
        report = inventory_active_tip(store, COMMIT, repository="owner/repo")
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
            repository="owner/repo",
            legacy_aliases={
                "legacy-luna": "gpt-5.6-luna",
                "legacy-sol": "gpt-5.6-sol",
            },
        )[0]
        implementer = report["provenance"]["implementer"]
        reviewer = report["provenance"]["reviewer"]
        self.assertEqual("legacy-luna", implementer["model_id"])
        self.assertEqual("gpt-5.6-luna", implementer["canonical_model"])
        self.assertFalse(implementer["immutable_lane_output"])
        self.assertFalse(reviewer["immutable_lane_output"])
        self.assertFalse(report["migration_complete"])
        self.assertTrue(report["migration_decision"]["replacement_required"])
        self.assertTrue(report["migration_decision"]["retirement_required"])

    def test_legacy_inventory_uses_schema_v1_family_fields_as_model_declarations(self):
        from pr_closure.migration import inventory_active_tip

        root = Path(tempfile.mkdtemp(prefix="policy-inventory-family-model-", dir="/var/tmp"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        store = RunStore(root, "project", 7)
        tip = root / "tip.json"
        tip.write_text("tip")
        store.record_commit(COMMIT, str(tip))
        store.write_review(
            COMMIT,
            "family-models",
            {
                "schema_version": 1,
                "repository": "owner/repo",
                "pr_number": 7,
                "reviewed_commit": COMMIT,
                "implementer_family": "gpt-5.6-luna",
                "reviewer_family": "gpt-5.6-sol",
                "verdict": "APPROVED",
            },
        )

        report = inventory_active_tip(store, COMMIT, repository="owner/repo")[0]
        implementer = report["provenance"]["implementer"]
        reviewer = report["provenance"]["reviewer"]
        self.assertEqual("gpt-5.6-luna", implementer["original_declaration"])
        self.assertEqual("gpt-5.6-luna", implementer["model_id"])
        self.assertEqual("gpt-5.6-luna", implementer["canonical_model"])
        self.assertEqual("openai", implementer["resolved_family"])
        self.assertEqual("gpt-5.6-sol", reviewer["canonical_model"])
        self.assertEqual("openai", reviewer["resolved_family"])

    def test_legacy_inventory_accepts_only_a_complete_immutable_lane_chain(self):
        from pr_closure.migration import inventory_active_tip

        root = Path(tempfile.mkdtemp(prefix="policy-inventory-full-chain-", dir="/var/tmp"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        store = RunStore(root, "project", 7)
        tip = root / "tip.json"
        tip.write_text("tip")
        store.record_commit(COMMIT, str(tip))
        record = {
            "schema_version": 1,
            "repository": "owner/repo",
            "pr_number": 7,
            "reviewed_commit": COMMIT,
            "implementer_family": "gpt-5.6-luna",
            "reviewer_family": "gpt-5.6-sol",
            "verdict": "APPROVED",
        }
        provenance = {}
        for role, model_id in (("implementer", "gpt-5.6-luna"), ("reviewer", "gpt-5.6-sol")):
            output_path = root / "outputs" / (role + ".out")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes((role + " output").encode())
            output_digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
            run_ref = "orchestration://run/legacy/{0}".format(role)
            manifest_path = root / "runs" / "legacy" / role / "manifest.json"
            manifest = {
                "schema_version": 1,
                "run_ref": run_ref,
                "repository": record["repository"],
                "pr_number": record["pr_number"],
                "reviewed_commit": COMMIT,
                "registry_version": "models-v1",
                "launcher_registry_version": "launchers-v1",
                "model_id": model_id,
                "runner": "codex",
                "invocation_model": model_id,
                "producer_output_path": str(output_path),
                "producer_output_sha256": output_digest,
            }
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            manifest_path.write_bytes(manifest_raw)
            manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
            envelope = dict(manifest)
            envelope.update({
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_digest,
            })
            envelope_path = root / "provenance" / (role + ".json")
            envelope_path.parent.mkdir(parents=True, exist_ok=True)
            envelope_raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
            envelope_path.write_bytes(envelope_raw)
            provenance[role] = {
                "model_id": model_id,
                "runner": "codex",
                "invocation_model": model_id,
                "run_ref": run_ref,
                "durable_path": str(envelope_path),
                "sha256": hashlib.sha256(envelope_raw).hexdigest(),
            }
        record["provenance"] = provenance
        store.write_review(COMMIT, "complete", record)

        report = inventory_active_tip(store, COMMIT, repository="owner/repo")[0]
        self.assertFalse(report["migration_complete"])
        self.assertEqual("replace-and-retire", report["migration_decision"]["action"])
        self.assertTrue(report["migration_decision"]["replacement_required"])
        self.assertTrue(report["migration_decision"]["retirement_required"])
        self.assertTrue(report["provenance"]["implementer"]["immutable_lane_output"])
        self.assertTrue(report["provenance"]["reviewer"]["immutable_lane_output"])

    def test_legacy_inventory_rejects_wrong_target_identity_and_non_integer_schema(self):
        from pr_closure.migration import inventory_active_tip

        for variant in ("wrong-pr", "boolean-schema"):
            with self.subTest(variant=variant):
                root = Path(tempfile.mkdtemp(prefix="policy-inventory-identity-", dir="/var/tmp"))
                self.addCleanup(lambda root=root: shutil.rmtree(root, ignore_errors=True))
                store = RunStore(root, "project", 7)
                record = _complete_legacy_record(
                    root,
                    store,
                    pr_number=8 if variant == "wrong-pr" else 7,
                    schema_version=True if variant == "boolean-schema" else 1,
                )
                store.write_review(COMMIT, variant, record)

                report = inventory_active_tip(store, COMMIT, repository="owner/repo")[0]
                self.assertFalse(report["provenance"]["implementer"]["verified"])
                self.assertFalse(report["provenance"]["reviewer"]["verified"])
                self.assertFalse(report["provenance"]["implementer"]["immutable_lane_output"])
                self.assertFalse(report["provenance"]["reviewer"]["immutable_lane_output"])

    def test_legacy_inventory_rejects_boolean_chain_schema_and_pr_fields(self):
        from pr_closure.migration import inventory_active_tip

        variants = (
            ("boolean-chain-schema", 7, 7, 1, True, None),
            ("boolean-chain-pr", 1, 1, 1, 1, True),
        )
        for name, store_pr, record_pr, record_schema, chain_schema, chain_pr in variants:
            with self.subTest(variant=name):
                root = Path(tempfile.mkdtemp(prefix="policy-inventory-chain-types-", dir="/var/tmp"))
                self.addCleanup(lambda root=root: shutil.rmtree(root, ignore_errors=True))
                store = RunStore(root, "project", store_pr)
                record = _complete_legacy_record(
                    root,
                    store,
                    pr_number=record_pr,
                    schema_version=record_schema,
                    chain_schema_version=chain_schema,
                    chain_pr_number=chain_pr,
                )
                store.write_review(COMMIT, name, record)

                report = inventory_active_tip(
                    store, COMMIT, repository="owner/repo"
                )[0]
                self.assertFalse(report["provenance"]["implementer"]["verified"])
                self.assertFalse(report["provenance"]["reviewer"]["verified"])
                self.assertFalse(report["provenance"]["implementer"]["immutable_lane_output"])
                self.assertFalse(report["provenance"]["reviewer"]["immutable_lane_output"])

    def test_inventory_binds_schema_v2_records_to_repository_and_store_pr(self):
        from pr_closure.migration import inventory_active_tip

        config = validate_project_config(active_policy_config())
        for variant, record_repository, record_pr in (
            ("wrong-repository", "other/repo", 7),
            ("wrong-pr", "owner/repo", 8),
        ):
            with self.subTest(variant=variant):
                root = Path(tempfile.mkdtemp(prefix="policy-inventory-v2-target-", dir="/var/tmp"))
                self.addCleanup(lambda root=root: shutil.rmtree(root, ignore_errors=True))
                store = RunStore(root, "project", 7)
                record = _complete_v2_record(
                    root, repository=record_repository, pr_number=record_pr
                )
                store.write_review(COMMIT, variant, record)

                report = inventory_active_tip(
                    store,
                    COMMIT,
                    repository="owner/repo",
                    review_policy=config.review_policy,
                    model_routes=config.model_routes,
                )[0]
                self.assertFalse(report["migration_complete"])
                self.assertEqual("replace-and-retire", report["migration_decision"]["action"])
                self.assertFalse(report["provenance"]["implementer"]["verified"])
                self.assertFalse(report["provenance"]["reviewer"]["verified"])

    def test_review_json_only_outputs_are_never_immutable_lane_evidence(self):
        from pr_closure.migration import inventory_active_tip

        root = Path(tempfile.mkdtemp(prefix="policy-inventory-json-only-", dir="/var/tmp"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        store = RunStore(root, "project", 7)
        tip = root / "tip.json"
        tip.write_text("tip")
        store.record_commit(COMMIT, str(tip))
        output = root / "review-json-only.out"
        output.write_bytes(b"review JSON claimed output")
        store.write_review(
            COMMIT,
            "json-only",
            {
                "schema_version": 1,
                "repository": "owner/repo",
                "pr_number": 7,
                "reviewed_commit": COMMIT,
                "implementer_family": "gpt-5.6-luna",
                "reviewer_family": "gpt-5.6-sol",
                "verdict": "APPROVED",
                "producer_outputs": {
                    "implementer": {
                        "producer_output_path": str(output),
                        "producer_output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    },
                    "reviewer": {
                        "producer_output_path": str(output),
                        "producer_output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    },
                },
            },
        )

        report = inventory_active_tip(store, COMMIT, repository="owner/repo")[0]
        self.assertFalse(report["provenance"]["implementer"]["immutable_lane_output"])
        self.assertFalse(report["provenance"]["reviewer"]["immutable_lane_output"])
        self.assertEqual("replace-and-retire", report["migration_decision"]["action"])

    def test_governing_docs_distinguish_default_cross_family_from_exact_same_family_exception(self):
        root = Path(__file__).resolve().parents[2]
        playbook = (root / "PLAYBOOK.md").read_text()
        readme = (root / "README.md").read_text()
        governing = playbook + "\n" + readme
        self.assertIn("OpenAI GPT-5.6 Luna implementer", governing)
        self.assertIn("OpenAI GPT-5.6 Sol reviewer", governing)
        self.assertIn("separate independent review pass", governing)
        self.assertNotIn("OpenAI-family reviewer may cover a DeepSeek implementation, but never an OpenAI-family implementation", governing)
        self.assertNotIn("reviewer must be a different model family", governing)
        self.assertNotIn("independent (cross-family) review", governing)
        self.assertIn("Claude Code is a supported agent runtime host", governing)

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
