import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pr_closure.provenance import ProvenanceValidationError, verify_review_provenance
from pr_closure.review import validate_review
from tools.tests.test_policy_config import active_policy_config
from tools.tests.test_review_policy import v2_record
from pr_closure.contract import validate_project_config


def write_json(path: Path, value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "closure"
        self.root.mkdir()
        config = validate_project_config(active_policy_config())
        raw = v2_record()
        for role in ("implementer", "reviewer"):
            participant = raw["provenance"][role]
            manifest = {
                "schema_version": 1,
                "run_ref": participant["run_ref"],
                "repository": raw["repository"],
                "pr_number": raw["pr_number"],
                "reviewed_commit": raw["reviewed_commit"],
                "registry_version": raw["provenance"]["registry_version"],
                "launcher_registry_version": raw["provenance"][
                    "launcher_registry_version"
                ],
                "model_id": participant["model_id"],
                "runner": participant["runner"],
                "invocation_model": participant["invocation_model"],
                "producer_output_sha256": ("2" if role == "implementer" else "3") * 64,
            }
            manifest_path = self.root / "runs" / (role + "-manifest.json")
            manifest_sha = write_json(manifest_path, manifest)
            envelope = dict(manifest)
            envelope.update({
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha,
            })
            envelope_path = self.root / "provenance" / (role + ".json")
            participant["durable_path"] = str(envelope_path)
            participant["sha256"] = write_json(envelope_path, envelope)
        self.raw = raw
        self.record = validate_review(
            raw,
            review_policy=config.review_policy,
            model_routes=config.model_routes,
        )

    def test_both_envelopes_and_manifests_verify(self):
        verify_review_provenance(self.record, self.root)

    def test_changed_missing_or_outside_envelope_fails(self):
        participant = self.record.provenance.implementer
        path = Path(participant.durable_path)
        original = path.read_bytes()
        for mutation in ("changed", "missing", "outside"):
            path.write_bytes(original)
            if mutation == "changed":
                path.write_bytes(original + b" ")
                record = self.record
            elif mutation == "missing":
                path.unlink()
                record = self.record
            else:
                outside = Path(self.temp.name) / "outside.json"
                outside.write_bytes(original)
                record = replace(
                    self.record,
                    provenance=replace(
                        self.record.provenance,
                        implementer=replace(participant, durable_path=str(outside)),
                    ),
                )
            with self.subTest(mutation=mutation):
                with self.assertRaises(ProvenanceValidationError):
                    verify_review_provenance(record, self.root)

    def test_symlink_envelope_fails(self):
        participant = self.record.provenance.implementer
        path = Path(participant.durable_path)
        target = path.with_name("target.json")
        path.rename(target)
        os.symlink(target, path)
        with self.assertRaises(ProvenanceValidationError):
            verify_review_provenance(self.record, self.root)

    def test_manifest_identity_or_digest_mismatch_fails(self):
        participant = self.record.provenance.reviewer
        envelope_path = Path(participant.durable_path)
        envelope = json.loads(envelope_path.read_text())
        manifest_path = Path(envelope["manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        manifest["runner"] = "jcode"
        write_json(manifest_path, manifest)
        with self.assertRaises(ProvenanceValidationError):
            verify_review_provenance(self.record, self.root)

    def test_manifest_path_shape_is_typed_and_fails_closed(self):
        participant = self.record.provenance.implementer
        envelope_path = Path(participant.durable_path)
        envelope = json.loads(envelope_path.read_text())
        envelope["manifest_path"] = None
        envelope_digest = write_json(envelope_path, envelope)
        record = replace(
            self.record,
            provenance=replace(
                self.record.provenance,
                implementer=replace(participant, sha256=envelope_digest),
            ),
        )
        with self.assertRaises(ProvenanceValidationError):
            verify_review_provenance(record, self.root)


if __name__ == "__main__":
    unittest.main()
