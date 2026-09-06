"""Advisory inventory of legacy active-tip review artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from pr_closure.provenance import ProvenanceValidationError, verify_review_provenance
from pr_closure.registries import MODEL_ALIASES, MODEL_REGISTRIES, require_model
from pr_closure.review import ReviewValidationError, validate_review
from pr_closure.secure_paths import SecurePathError, read_contained_file


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
        return provenance[role]
    for key in (f"{role}_provenance", f"{role}_lane", f"{role}_output"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    outputs = record.get("producer_outputs")
    if isinstance(outputs, dict) and isinstance(outputs.get(role), dict):
        return outputs[role]
    return {}


def _producer_output_inventory(participant, root):
    details = {
        "path": participant.get("producer_output_path"),
        "declared_sha256": participant.get("producer_output_sha256"),
        "exists": False,
        "digest_matches": False,
        "envelope_digest_matches": None,
    }
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
            envelope = json.loads(envelope_raw.decode("utf-8"))
            if isinstance(envelope, dict):
                details["path"] = envelope.get("producer_output_path")
                details["declared_sha256"] = envelope.get("producer_output_sha256")
        except (OSError, SecurePathError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    output_path = details["path"]
    if not isinstance(output_path, str):
        return details
    try:
        _output_raw, output_digest = read_contained_file(
            output_path, root, "legacy producer output"
        )
    except (OSError, SecurePathError):
        return details
    details["exists"] = True
    details["digest_matches"] = output_digest == details["declared_sha256"]
    return details


def _participant_inventory(record, role, registry_version, aliases, root):
    prefix = role + "_"
    participant = dict(_participant_source(record, role))
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
        participant.setdefault(name, record.get(prefix + name))
    declared_model = participant.get("model_id", record.get(prefix + "model"))
    resolved = _resolve_legacy_model(declared_model, registry_version, aliases)
    output = _producer_output_inventory(participant, root)
    verification_error = None
    if resolved["canonical"] is None and declared_model is not None:
        verification_error = "model declaration is not a released canonical ID or reviewed alias"
    return {
        "model_id": declared_model,
        "canonical_model": resolved["canonical"],
        "alias": resolved["alias"],
        "resolved_family": resolved["family"],
        "original_declaration": record.get(prefix + "family"),
        "family": record.get(prefix + "family"),
        "runner": participant.get("runner", record.get(prefix + "runner")),
        "invocation_model": participant.get(
            "invocation_model", record.get(prefix + "invocation_model")
        ),
        "durable_path": participant.get("durable_path", record.get(prefix + "durable_path")),
        "sha256": participant.get("sha256", record.get(prefix + "sha256")),
        "producer_output": output,
        "immutable_lane_output": bool(
            output["exists"]
            and output["digest_matches"]
            and output["envelope_digest_matches"] is not False
        ),
        "verified": False,
        "verification_error": verification_error,
    }


def inventory_active_tip(
    store,
    commit,
    *,
    legacy_aliases=None,
    closure_root=None,
    review_policy=None,
    model_routes=(),
):
    """Return every legacy artifact at ``commit`` without granting authority."""
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
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("legacy review record is not valid UTF-8 JSON") from error
        if not isinstance(record, dict):
            raise ValueError("legacy review record must be a JSON object")
        active_tip = record.get("reviewed_commit") == commit
        implementer = _participant_inventory(
            record, "implementer", registry_version, aliases, root
        )
        reviewer = _participant_inventory(
            record, "reviewer", registry_version, aliases, root
        )
        if record.get("schema_version") == 2:
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
        complete = active_tip and implementer["verified"] and reviewer["verified"]
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
