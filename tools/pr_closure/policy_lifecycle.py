"""Typed policy activation and rollback transition rules."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Mapping

from pr_closure.contract import ConfigValidationError, validate_project_config
from pr_closure.families import resolve_family
from pr_closure.model import (
    ModelRoute,
    ReviewPolicy,
    ReviewPolicyMode,
    SameFamilyReviewException,
)
from pr_closure.review import validate_review


_ROLLBACK_ROUTES = (
    {
        "id": "publyapp-luna-to-sol-v1",
        "registry_version": "models-v1",
        "launcher_registry_version": "launchers-v1",
        "implementer_model": "gpt-5.6-luna",
        "implementer_runner": "codex",
        "implementer_invocation_model": "gpt-5.6-luna",
        "reviewer_model": "deepseek-v4-flash",
        "reviewer_runner": "opencode",
        "reviewer_invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
        "same_family_policy_id": None,
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
)

ROLLBACK_RETIREMENT_POLICY_ID = "publyapp-gpt-implementation-sol-review-v1"
ROLLBACK_RETIREMENT_REASON = "policy-rollback: same-family-review-replaced"
ROLLBACK_OWNER_AUTHORIZATION = "Radan; owner instruction 2026-09-05"

_ROLLBACK_SOURCE_EXCEPTION = SameFamilyReviewException(
    id=ROLLBACK_RETIREMENT_POLICY_ID,
    registry_version="models-v1",
    implementer_family="openai",
    reviewer_model="gpt-5.6-sol",
    required_for_authorized_family=True,
    owner_authorization=ROLLBACK_OWNER_AUTHORIZATION,
    rationale="GPT implementation is reviewed by gpt-5.6-sol; Claude is forbidden.",
)
_ROLLBACK_SOURCE_POLICY = ReviewPolicy(
    mode=ReviewPolicyMode.STAGED,
    owner_authorization=ROLLBACK_OWNER_AUTHORIZATION,
    forbidden_reviewer_families=("anthropic",),
    same_family_exceptions=(_ROLLBACK_SOURCE_EXCEPTION,),
)
_ROLLBACK_SOURCE_ROUTES = tuple(
    ModelRoute(**route)
    for route in (
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
            "same_family_policy_id": ROLLBACK_RETIREMENT_POLICY_ID,
        },
    )
)


def _without_mode(raw: Mapping) -> dict:
    value = copy.deepcopy(dict(raw))
    policy = value.get("review_policy")
    if isinstance(policy, dict):
        policy.pop("mode", None)
    return value


def _route_as_mapping(route: ModelRoute) -> dict:
    return {name: getattr(route, name) for name in ModelRoute.__dataclass_fields__}


def policy_identity(config) -> tuple[str, str]:
    """Return the stable ID and digest for a policy, excluding its mode.

    ``staged`` and ``enforced`` are transition modes for the same policy
    content.  The project-scoped adoption record binds this content identity
    separately from each PR's full configuration digest.
    """
    policy = config.review_policy
    payload = {
        "owner_authorization": policy.owner_authorization,
        "forbidden_reviewer_families": list(policy.forbidden_reviewer_families),
        "same_family_exceptions": [
            {
                name: getattr(exception, name)
                for name in SameFamilyReviewException.__dataclass_fields__
            }
            for exception in policy.same_family_exceptions
        ],
        "model_routes": [
            _route_as_mapping(route)
            for route in config.model_routes
        ],
    }
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return "review-policy-" + digest, digest


def validate_adopted_policy_context(config, adoption: Mapping) -> None:
    """Reject config authority that does not match the adopted project policy.

    A staged exact rollback target is the only different policy permitted to
    exist while the owner-authorized rollback transition is being migrated.
    It remains non-authoritative until its own activation/adoption event.
    """
    if adoption.get("project") != config.project:
        raise ConfigValidationError("policy adoption project does not match configuration")
    if adoption.get("repository") != config.repository:
        raise ConfigValidationError("policy adoption repository does not match configuration")
    policy_id, digest = policy_identity(config)
    if adoption.get("policy_id") == policy_id and adoption.get("policy_digest") == digest:
        if (
            config.review_policy.mode is ReviewPolicyMode.ENFORCED
            and adoption.get("enforced_config_digest") != config.config_digest
        ):
            raise ConfigValidationError(
                "enforced configuration does not match the adopted transition"
            )
        return
    if config.review_policy.mode is ReviewPolicyMode.STAGED and is_exact_rollback_target(config):
        return
    raise ConfigValidationError(
        "configuration does not match the adopted project policy; policy removal or replacement is not a rollback"
    )


def policy_transition_kind(config, adoption: Mapping | None) -> str:
    """Return the authorized adoption transition for a staged config."""
    if adoption is None:
        return "initial-adoption"
    validate_adopted_policy_context(config, adoption)
    policy_id, digest = policy_identity(config)
    if adoption.get("policy_id") == policy_id and adoption.get("policy_digest") == digest:
        return "continued-adoption"
    if config.review_policy.mode is ReviewPolicyMode.STAGED and is_exact_rollback_target(config):
        return "authorized-rollback"
    raise ConfigValidationError("policy transition is not an authorized staged rollback")


def is_exact_rollback_target(config: Mapping) -> bool:
    """Return whether a normalized or raw config is the pinned rollback target."""
    normalized = config if hasattr(config, "review_policy") else validate_project_config(config)
    if normalized.project != "publyapp":
        return False
    if normalized.review_policy.mode is not ReviewPolicyMode.STAGED:
        return False
    if normalized.review_policy.owner_authorization != ROLLBACK_OWNER_AUTHORIZATION:
        return False
    if normalized.review_policy.forbidden_reviewer_families != ("anthropic",):
        return False
    if normalized.review_policy.same_family_exceptions:
        return False
    if tuple(_route_as_mapping(route) for route in normalized.model_routes) != _ROLLBACK_ROUTES:
        return False
    return True


def validate_activation_projection(staged: Mapping, projected: Mapping) -> None:
    """Validate the only transitions allowed to create enforced authority.

    The only transition that creates enforced authority changes only
    ``review_policy.mode``. Route and policy mutations must be staged before
    this check and are rejected here.
    """
    if not isinstance(staged, Mapping) or not isinstance(projected, Mapping):
        raise ConfigValidationError("activation projection requires two config objects")
    staged_config = validate_project_config(staged)
    projected_config = validate_project_config(projected)
    if staged_config.review_policy.mode is not ReviewPolicyMode.STAGED:
        raise ConfigValidationError("activation source must be staged")
    if projected_config.review_policy.mode is not ReviewPolicyMode.ENFORCED:
        raise ConfigValidationError("activation target must be enforced")
    if _without_mode(staged) == _without_mode(projected):
        return
    raise ConfigValidationError(
        "projected enforced config must change only review_policy.mode"
    )


def _retirement_review_bytes(store, commit, review_id, retirement_id, expected_sha256=None):
    candidates = [store.review_path(commit, review_id)]
    if retirement_id is not None:
        candidates.extend(
            (
                store.retirement_staging_path(commit, retirement_id),
                store.retirement_final_path(commit, retirement_id),
            )
        )
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            raw, digest = store._read_bound_bytes(path, "retirement review record")
        except FileNotFoundError:
            continue
        if expected_sha256 is not None and digest != expected_sha256:
            raise ConfigValidationError("retirement review digest does not match its immutable envelope")
        return raw
    raise ConfigValidationError("retirement source review does not exist and no immutable replay artifact is available")


def authorize_retirement(
    config,
    store,
    commit,
    review_id,
    reason,
    policy_id,
    retirement_id=None,
    expected_sha256=None,
) -> None:
    """Authorize one retirement under the normalized staged policy."""
    if config.review_policy.mode is not ReviewPolicyMode.STAGED:
        raise ConfigValidationError("review retirement requires a staged policy")
    replay_envelope = None
    if retirement_id is not None:
        replay_envelope = store._read_retirement_envelope(commit, retirement_id)
        if replay_envelope is not None:
            expected_arguments = {
                "repository": config.repository,
                "review_id": review_id,
                "source_path": str(store.review_path(commit, review_id).resolve()),
                "reason": reason,
                "policy_id": policy_id,
            }
            if expected_sha256 is not None:
                expected_arguments["expected_sha256"] = expected_sha256
            for key, value in expected_arguments.items():
                if replay_envelope.get(key) != value:
                    raise ConfigValidationError(
                        "retirement replay arguments do not match its immutable envelope"
                    )
    raw = _retirement_review_bytes(
        store,
        commit,
        review_id,
        retirement_id if replay_envelope is not None else None,
        expected_sha256,
    )
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigValidationError("retirement source is not valid UTF-8 JSON") from error
    if not isinstance(record, dict):
        raise ConfigValidationError("retirement source must be a JSON object")
    schema_version = record.get("schema_version")
    if schema_version == 1:
        try:
            legacy = validate_review(record)
            reviewer_family = resolve_family(legacy.reviewer_family)
        except (ValueError, ConfigValidationError) as error:
            raise ConfigValidationError("retirement source is not a valid legacy review") from error
        expected_reason = (
            "policy-migration: claude-reviewer-forbidden"
            if reviewer_family == "anthropic"
            else "policy-migration: schema-v2-provenance-required"
        )
        if (
            legacy.repository != config.repository
            or legacy.pr_number != store.pr
            or legacy.reviewed_commit != commit
        ):
            raise ConfigValidationError("retirement source identity is not bound to this target")
        if reason != expected_reason:
            raise ConfigValidationError(
                "retirement reason does not apply to the legacy source reviewer"
            )
        return
    if schema_version == 2:
        if is_exact_rollback_target(config):
            if policy_id != ROLLBACK_RETIREMENT_POLICY_ID or reason != ROLLBACK_RETIREMENT_REASON:
                raise ConfigValidationError(
                    "rollback retirement requires its exact policy, reason, and target"
                )
            try:
                current = validate_review(
                    record,
                    review_policy=_ROLLBACK_SOURCE_POLICY,
                    model_routes=_ROLLBACK_SOURCE_ROUTES,
                )
            except ValueError as error:
                raise ConfigValidationError(
                    "rollback retirement source is not the pinned old same-family review"
                ) from error
            if (
                current.implementer_model != "gpt-5.6-luna"
                or current.reviewer_model != "gpt-5.6-sol"
                or current.review_exception_id != ROLLBACK_RETIREMENT_POLICY_ID
            ):
                raise ConfigValidationError("rollback retirement source identity is not exact")
            if (
                current.repository != config.repository
                or current.pr_number != store.pr
                or current.reviewed_commit != commit
            ):
                raise ConfigValidationError(
                    "rollback retirement source identity is not bound to this target"
                )
            return
        configured_ids = {
            exception.id for exception in config.review_policy.same_family_exceptions
        }
        if policy_id not in configured_ids:
            raise ConfigValidationError("retirement policy_id is not configured")
        try:
            current = validate_review(
                record,
                review_policy=config.review_policy,
                model_routes=config.model_routes,
            )
        except ValueError as error:
            raise ConfigValidationError("retirement source is not policy-valid schema v2") from error
        if (
            current.implementer_family != current.reviewer_family
            or current.review_exception_id != policy_id
            or reason != "policy-rollback: same-family-review-replaced"
        ):
            raise ConfigValidationError(
                "retirement reason does not apply to the schema-v2 source policy identity"
            )
        if (
            current.repository != config.repository
            or current.pr_number != store.pr
            or current.reviewed_commit != commit
        ):
            raise ConfigValidationError("retirement source identity is not bound to this target")
        return
    raise ConfigValidationError("retirement source schema_version is not migratable")
