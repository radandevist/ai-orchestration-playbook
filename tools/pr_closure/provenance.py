from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from pr_closure.model import ProvenanceParticipant, ReviewRecord
from pr_closure.secure_paths import SecurePathError, read_contained_file


class ProvenanceValidationError(ValueError):
    """Raised when durable model provenance cannot be independently verified."""


_DIGEST_KEYS = frozenset({"producer_output_sha256", "manifest_sha256"})
_MANIFEST_KEYS = frozenset({
    "schema_version",
    "run_ref",
    "repository",
    "pr_number",
    "reviewed_commit",
    "registry_version",
    "launcher_registry_version",
    "model_id",
    "runner",
    "invocation_model",
    "producer_output_path",
    "producer_output_sha256",
})
_ENVELOPE_KEYS = _MANIFEST_KEYS | frozenset({"manifest_path", "manifest_sha256"})


def _read_bound(path_value: str, root_value: str, label: str) -> tuple[bytes, str]:
    try:
        return read_contained_file(path_value, root_value, label)
    except (OSError, SecurePathError) as error:
        raise ProvenanceValidationError(
            "cannot open {0}: {1}".format(label, error)
        ) from error


def _manifest_path(closure_root: Path, run_ref: str) -> str:
    prefix = "orchestration://run/"
    if not isinstance(run_ref, str) or not run_ref.startswith(prefix):
        raise ProvenanceValidationError("run_ref must use the authoritative orchestration://run/ form")
    relative = run_ref[len(prefix):]
    parts = tuple(relative.split("/"))
    if not parts or any(
        not part or part in (".", "..") or "\\" in part or "\x00" in part
        for part in parts
    ):
        raise ProvenanceValidationError("run_ref contains an unsafe manifest component")
    return os.path.abspath(os.path.join(os.fspath(closure_root), "runs", *parts, "manifest.json"))


def _json_object(raw: bytes, label: str) -> Mapping:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProvenanceValidationError(label + " is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ProvenanceValidationError(label + " must be a JSON object")
    return value


def _exact_keys(value: Mapping, keys: frozenset, label: str) -> None:
    actual = frozenset(value)
    if actual != keys:
        raise ProvenanceValidationError(
            "{0} keys differ: missing={1}, unknown={2}".format(
                label,
                sorted(keys - actual),
                sorted(actual - keys),
            )
        )


def _require_digest(value, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ProvenanceValidationError(label + " must be lowercase 64-hex")
    return value


def _expected_identity(
    record: ReviewRecord,
    participant: ProvenanceParticipant,
) -> dict:
    provenance = record.provenance
    if provenance is None:
        raise ProvenanceValidationError("schema-v2 review has no provenance")
    return {
        "run_ref": participant.run_ref,
        "repository": record.repository,
        "pr_number": record.pr_number,
        "reviewed_commit": record.reviewed_commit,
        "registry_version": provenance.registry_version,
        "launcher_registry_version": provenance.launcher_registry_version,
        "model_id": participant.model_id,
        "runner": participant.runner,
        "invocation_model": participant.invocation_model,
    }


def _verify_participant(
    record: ReviewRecord,
    participant: ProvenanceParticipant,
    closure_root: Path,
    label: str,
) -> None:
    envelope_raw, envelope_digest = _read_bound(
        participant.durable_path,
        os.fspath(closure_root),
        label + " envelope",
    )
    if envelope_digest != participant.sha256:
        raise ProvenanceValidationError(label + " envelope digest mismatch")
    envelope = _json_object(envelope_raw, label + " envelope")
    _exact_keys(envelope, _ENVELOPE_KEYS, label + " envelope")
    if envelope.get("schema_version") != 1 or isinstance(
        envelope.get("schema_version"), bool
    ):
        raise ProvenanceValidationError(label + " envelope schema_version must be 1")
    expected = _expected_identity(record, participant)
    for key, value in expected.items():
        if envelope.get(key) != value:
            raise ProvenanceValidationError(
                "{0} envelope {1} does not match review".format(label, key)
            )
    producer_digest = _require_digest(
        envelope.get("producer_output_sha256"),
        label + " producer_output_sha256",
    )
    manifest_path = envelope.get("manifest_path")
    manifest_digest = _require_digest(
        envelope.get("manifest_sha256"), label + " manifest_sha256"
    )
    manifest_raw, actual_manifest_digest = _read_bound(
        manifest_path,
        os.fspath(closure_root),
        label + " manifest",
    )
    if manifest_digest != actual_manifest_digest:
        raise ProvenanceValidationError(label + " manifest digest mismatch")
    manifest = _json_object(manifest_raw, label + " manifest")
    _exact_keys(manifest, _MANIFEST_KEYS, label + " manifest")
    if manifest.get("schema_version") != 1 or isinstance(
        manifest.get("schema_version"), bool
    ):
        raise ProvenanceValidationError(label + " manifest schema_version must be 1")
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ProvenanceValidationError(
                "{0} manifest {1} does not match review".format(label, key)
            )
    if manifest.get("producer_output_sha256") != producer_digest:
        raise ProvenanceValidationError(label + " producer output digest mismatch")
    expected_manifest_path = _manifest_path(closure_root, participant.run_ref)
    if manifest_path != expected_manifest_path:
        raise ProvenanceValidationError(label + " manifest is not the authoritative run manifest")
    producer_path = envelope.get("producer_output_path")
    if producer_path != manifest.get("producer_output_path"):
        raise ProvenanceValidationError(label + " producer output path mismatch")
    _output_raw, actual_output_digest = _read_bound(
        producer_path,
        os.fspath(closure_root),
        label + " producer output",
    )
    if actual_output_digest != producer_digest:
        raise ProvenanceValidationError(label + " producer output digest is not independently verified")


def verify_review_provenance(record: ReviewRecord, closure_root: Path) -> None:
    provenance = record.provenance
    if record.schema_version != 2 or provenance is None:
        raise ProvenanceValidationError("only schema-v2 reviews carry model provenance")
    _verify_participant(record, provenance.implementer, closure_root, "implementer")
    _verify_participant(record, provenance.reviewer, closure_root, "reviewer")
