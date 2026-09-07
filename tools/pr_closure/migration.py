"""Advisory inventory of legacy active-tip review artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from pr_closure.provenance import (
    ProvenanceValidationError,
    _ENVELOPE_KEYS,
    _MANIFEST_KEYS,
    _manifest_path,
    verify_review_provenance,
)
from pr_closure.registries import (
    MODEL_ALIASES,
    MODEL_REGISTRIES,
    require_launcher,
    require_model,
)
from pr_closure.review import ReviewValidationError, validate_review
from pr_closure.secure_paths import SecurePathError, read_contained_file
from pr_closure.jsonio import StrictJsonError, loads as strict_json_loads


def _resolve_legacy_model(value, registry_version, aliases):
    original = value
    if not isinstance(value, str) or not value:
        return {
            "original": original,
            "canonical": None,
            "alias": None,
            "family": None,
        }
    registry = MODEL_REGISTRIES.get(registry_version, {})
    if value in registry:
        canonical = value
        alias = None
    elif value in aliases:
        canonical = aliases[value]
        alias = value
    else:
        return {
            "original": original,
            "canonical": None,
            "alias": None,
            "family": None,
        }
    try:
        family = require_model(registry_version, canonical)
    except ValueError:
        return {
            "original": original,
            "canonical": None,
            "alias": alias,
            "family": None,
        }
    return {
        "original": original,
        "canonical": canonical,
        "alias": alias,
        "family": family,
    }


def _participant_source(record, role):
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and isinstance(provenance.get(role), dict):
        return provenance[role], "durable-participant"
    for key in (f"{role}_provenance", f"{role}_lane", f"{role}_output"):
        value = record.get(key)
        if isinstance(value, dict):
            return value, "durable-participant"
    outputs = record.get("producer_outputs")
    if isinstance(outputs, dict) and isinstance(outputs.get(role), dict):
        return outputs[role], "review-json-only"
    return {}, "record"


def _first_present(mapping, *keys):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _producer_output_inventory(participant, root, source_kind):
    details = {
        "path": participant.get("producer_output_path"),
        "declared_sha256": participant.get("producer_output_sha256"),
        "exists": False,
        "digest_matches": False,
        "envelope_digest_matches": False,
        "manifest_digest_matches": False,
    }
    if source_kind == "review-json-only":
        # A review JSON producer_outputs object is an untrusted claim. Even
        # matching bytes cannot supply the missing durable envelope, manifest,
        # runner, and invocation identity chain.
        output_path = details["path"]
        if isinstance(output_path, str) and root is not None:
            try:
                _output_raw, output_digest = read_contained_file(
                    output_path, root, "legacy producer output"
                )
                details["exists"] = True
                details["digest_matches"] = output_digest == details["declared_sha256"]
            except (OSError, SecurePathError, TypeError):
                pass
        return details
    durable_path = participant.get("durable_path")
    if root is not None and isinstance(durable_path, str):
        try:
            envelope_raw, envelope_digest = read_contained_file(
                durable_path, root, "legacy provenance envelope"
            )
            declared_envelope_digest = participant.get("sha256")
            details["envelope_digest_matches"] = (
                isinstance(declared_envelope_digest, str)
                and envelope_digest == declared_envelope_digest
            )
            envelope = strict_json_loads(envelope_raw, "legacy provenance envelope")
            if isinstance(envelope, dict):
                details["path"] = envelope.get("producer_output_path")
                details["declared_sha256"] = envelope.get("producer_output_sha256")
        except (
            OSError,
            SecurePathError,
            TypeError,
            UnicodeDecodeError,
            StrictJsonError,
        ):
            pass
    output_path = details["path"]
    if not isinstance(output_path, str):
        return details
    try:
        _output_raw, output_digest = read_contained_file(
            output_path, root, "legacy producer output"
        )
    except (OSError, SecurePathError, TypeError):
        return details
    details["exists"] = True
    details["digest_matches"] = output_digest == details["declared_sha256"]
    return details


def _verify_legacy_output_chain(
    record,
    role,
    participant,
    root,
    canonical_model,
    output,
    expected_repository,
    expected_pr_number,
):
    """Verify the complete immutable lane chain for a schema-v1 artifact."""
    if root is None or not isinstance(canonical_model, str):
        raise ValueError("missing closure root or canonical model declaration")
    if record.get("repository") != expected_repository:
        raise ValueError("review repository does not bind to the inventory target")
    pr_number = record.get("pr_number")
    if (
        not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number != expected_pr_number
    ):
        raise ValueError("review PR does not bind to the inventory target")
    if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        raise ValueError("review schema_version must be the exact integer 1")
    required = (
        "run_ref",
        "durable_path",
        "sha256",
        "runner",
        "invocation_model",
    )
    if any(not isinstance(participant.get(key), str) for key in required):
        raise ValueError("durable participant identity is incomplete")
    envelope_raw, envelope_digest = read_contained_file(
        participant["durable_path"], root, role + " legacy provenance envelope"
    )
    if envelope_digest != participant["sha256"]:
        raise ValueError("provenance envelope digest mismatch")
    envelope = strict_json_loads(envelope_raw, "legacy provenance envelope")
    if not isinstance(envelope, dict) or frozenset(envelope) != _ENVELOPE_KEYS:
        raise ValueError("provenance envelope keys are not exact")
    if type(envelope.get("schema_version")) is not int or envelope["schema_version"] != 1:
        raise ValueError("provenance envelope schema_version must be the exact integer 1")
    if (
        type(envelope.get("pr_number")) is not int
        or envelope["pr_number"] != expected_pr_number
    ):
        raise ValueError("provenance envelope pr_number must bind to the inventory target")
    registry_version = envelope.get("registry_version")
    launcher_registry_version = envelope.get("launcher_registry_version")
    expected = {
        "schema_version": 1,
        "run_ref": participant["run_ref"],
        "repository": record.get("repository"),
        "pr_number": record.get("pr_number"),
        "reviewed_commit": record.get("reviewed_commit"),
        "registry_version": registry_version,
        "launcher_registry_version": launcher_registry_version,
        "model_id": canonical_model,
        "runner": participant["runner"],
        "invocation_model": participant["invocation_model"],
    }
    if any(envelope.get(key) != value for key, value in expected.items()):
        raise ValueError("provenance envelope identity does not match review")
    require_launcher(
        registry_version,
        launcher_registry_version,
        canonical_model,
        participant["runner"],
        participant["invocation_model"],
    )
    manifest_path = envelope.get("manifest_path")
    manifest_digest = envelope.get("manifest_sha256")
    if not isinstance(manifest_path, str) or not isinstance(manifest_digest, str):
        raise ValueError("provenance envelope manifest binding is incomplete")
    manifest_raw, actual_manifest_digest = read_contained_file(
        manifest_path, root, role + " authoritative run manifest"
    )
    if actual_manifest_digest != manifest_digest:
        raise ValueError("authoritative run manifest digest mismatch")
    manifest = strict_json_loads(manifest_raw, "authoritative run manifest")
    if not isinstance(manifest, dict) or frozenset(manifest) != _MANIFEST_KEYS:
        raise ValueError("authoritative run manifest keys are not exact")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("authoritative run manifest schema_version must be the exact integer 1")
    if (
        type(manifest.get("pr_number")) is not int
        or manifest["pr_number"] != expected_pr_number
    ):
        raise ValueError("authoritative run manifest pr_number must bind to the inventory target")
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("authoritative run manifest identity does not match review")
    expected_manifest_path = _manifest_path(root, participant["run_ref"])
    if manifest_path != expected_manifest_path:
        raise ValueError("manifest is not the authoritative orchestration manifest")
    producer_path = envelope.get("producer_output_path")
    producer_digest = envelope.get("producer_output_sha256")
    if (
        producer_path != manifest.get("producer_output_path")
        or producer_digest != manifest.get("producer_output_sha256")
    ):
        raise ValueError("producer output binding differs between envelope and manifest")
    if (
        participant.get("producer_output_path") is not None
        and participant.get("producer_output_path") != producer_path
    ):
        raise ValueError("review producer output path differs from authoritative chain")
    if (
        participant.get("producer_output_sha256") is not None
        and participant.get("producer_output_sha256") != producer_digest
    ):
        raise ValueError("review producer output digest differs from authoritative chain")
    actual_output = read_contained_file(
        producer_path, root, role + " authoritative producer output"
    )
    if actual_output[1] != producer_digest:
        raise ValueError("authoritative producer output digest mismatch")
    output.update(
        {
            "path": producer_path,
            "declared_sha256": producer_digest,
            "exists": True,
            "digest_matches": True,
            "envelope_digest_matches": True,
            "manifest_digest_matches": True,
        }
    )


def _participant_inventory(
    record, role, registry_version, aliases, root, expected_repository, expected_pr_number
):
    prefix = role + "_"
    source, source_kind = _participant_source(record, role)
    participant = dict(source)
    for name in (
        "model_id",
        "runner",
        "invocation_model",
        "run_ref",
        "durable_path",
        "sha256",
        "producer_output_path",
        "producer_output_sha256",
    ):
        if participant.get(name) is None:
            participant[name] = record.get(prefix + name)
    declared_model = _first_present(
        participant,
        "model_id",
        "model",
    )
    if declared_model is None:
        declared_model = _first_present(
            record,
            prefix + "model",
            # Schema-v1 commonly called model declarations *_family. Keep the
            # raw value intact, then resolve it honestly against the registry.
            prefix + "family",
        )
    resolved = _resolve_legacy_model(declared_model, registry_version, aliases)
    output = _producer_output_inventory(participant, root, source_kind)
    verification_error = None
    if resolved["canonical"] is None and declared_model is not None:
        verification_error = "model declaration is not a released canonical ID or reviewed alias"
    schema_version = record.get("schema_version")
    if (
        verification_error is None
        and (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
        )
    ):
        verification_error = "schema_version must be an exact integer"
    verified = False
    if verification_error is None and schema_version == 1:
        try:
            _verify_legacy_output_chain(
                record,
                role,
                participant,
                root,
                resolved["canonical"],
                output,
                expected_repository,
                expected_pr_number,
            )
            verified = True
        except (
            OSError,
            SecurePathError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            verification_error = "immutable lane output chain is incomplete: {0}".format(
                error
            )
    return {
        "model_id": declared_model,
        "canonical_model": resolved["canonical"],
        "alias": resolved["alias"],
        "resolved_family": resolved["family"],
        "original_declaration": declared_model,
        "family": record.get(prefix + "family"),
        "runner": _first_present(participant, "runner")
        if participant.get("runner") is not None
        else record.get(prefix + "runner"),
        "invocation_model": _first_present(participant, "invocation_model")
        if participant.get("invocation_model") is not None
        else record.get(prefix + "invocation_model"),
        "durable_path": participant.get("durable_path"),
        "sha256": participant.get("sha256"),
        "producer_output": output,
        "immutable_lane_output": verified,
        "verified": verified,
        "verification_error": verification_error,
    }


def inventory_active_tip(
    store,
    commit,
    *,
    repository,
    legacy_aliases=None,
    closure_root=None,
    review_policy=None,
    model_routes=(),
):
    """Return every legacy artifact at ``commit`` without granting authority."""
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("authoritative repository is required for legacy inventory")
    registry_version = "models-v1"
    aliases = dict(
        MODEL_ALIASES.get(registry_version, {})
        if legacy_aliases is None
        else legacy_aliases
    )
    root = Path(closure_root) if closure_root is not None else Path(store._root)
    report = []
    for path in store.review_paths(commit):
        raw, raw_sha256 = store._read_bound_bytes(path, "legacy review record")
        try:
            record = strict_json_loads(raw, "legacy review record")
        except StrictJsonError as error:
            raise ValueError("legacy review record is not valid UTF-8 JSON") from error
        if not isinstance(record, dict):
            raise ValueError("legacy review record must be a JSON object")
        active_tip = record.get("reviewed_commit") == commit
        implementer = _participant_inventory(
            record,
            "implementer",
            registry_version,
            aliases,
            root,
            repository,
            store.pr,
        )
        reviewer = _participant_inventory(
            record,
            "reviewer",
            registry_version,
            aliases,
            root,
            repository,
            store.pr,
        )
        target_identity_error = None
        if record.get("repository") != repository:
            target_identity_error = "review repository does not bind to the inventory target"
        elif (
            type(record.get("pr_number")) is not int
            or record["pr_number"] != store.pr
        ):
            target_identity_error = "review PR does not bind to the inventory target"
        if record.get("schema_version") == 2 and target_identity_error is None:
            try:
                validated = validate_review(
                    record,
                    review_policy=review_policy,
                    model_routes=model_routes,
                )
                verify_review_provenance(validated, root)
                implementer["verified"] = True
                reviewer["verified"] = True
                implementer["immutable_lane_output"] = True
                reviewer["immutable_lane_output"] = True
                implementer["verification_error"] = None
                reviewer["verification_error"] = None
            except (ReviewValidationError, ProvenanceValidationError, ValueError) as error:
                implementer["verification_error"] = str(error)
                reviewer["verification_error"] = str(error)
        elif record.get("schema_version") == 2:
            implementer["verification_error"] = target_identity_error
            reviewer["verification_error"] = target_identity_error
        complete = (
            active_tip
            and record.get("schema_version") == 2
            and implementer["verified"]
            and reviewer["verified"]
        )
        if not active_tip:
            decision = {
                "action": "ignore-historical",
                "replacement": "not-required",
                "retirement": "already-historical",
                "replacement_required": False,
                "retirement_required": False,
            }
        elif complete:
            decision = {
                "action": "retain",
                "replacement": "verified-schema-v2",
                "retirement": "not-required",
                "replacement_required": False,
                "retirement_required": False,
            }
        else:
            decision = {
                "action": "replace-and-retire",
                "replacement": "fresh-schema-v2-review-required",
                "retirement": "retire-after-replacement",
                "replacement_required": True,
                "retirement_required": True,
            }
        report.append(
            {
                "review_id": path.stem,
                "schema_version": record.get("schema_version"),
                "raw_sha256": raw_sha256,
                "active_tip": active_tip,
                "authority": False,
                "source_path": str(path),
                "provenance": {
                    "implementer": implementer,
                    "reviewer": reviewer,
                },
        "migration_complete": complete,
        "migration_decision": decision,
            }
        )
    return tuple(report)
