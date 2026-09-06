"""Typed policy activation and rollback transition rules."""

from __future__ import annotations

import copy
import json
from typing import Mapping

from pr_closure.contract import ConfigValidationError, validate_project_config
from pr_closure.families import resolve_family
from pr_closure.model import ReviewPolicyMode
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


def _without_mode(raw: Mapping) -> dict:
    value = copy.deepcopy(dict(raw))
    policy = value.get("review_policy")
    if isinstance(policy, dict):
        policy.pop("mode", None)
    return value


def _rollback_is_exact(staged: Mapping, projected: Mapping) -> bool:
    if projected.get("model_routes") != list(_ROLLBACK_ROUTES):
        return False
    staged_policy = staged.get("review_policy")
    projected_policy = projected.get("review_policy")
    if not isinstance(staged_policy, dict) or not isinstance(projected_policy, dict):
        return False
    if projected_policy.get("forbidden_reviewer_families") != ["anthropic"]:
        return False
    if projected_policy.get("same_family_exceptions") != []:
        return False
    for key in ("owner_authorization",):
        if projected_policy.get(key) != staged_policy.get(key):
            return False
    return True


def validate_activation_projection(staged: Mapping, projected: Mapping) -> None:
    """Validate the only transitions allowed to create enforced authority.

    The ordinary transition changes only ``review_policy.mode``.  The one
    approved rollback is pinned to the registered Luna/DeepSeek routes below;
    it keeps Anthropic forbidden and removes the old same-family exception.
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
    if _rollback_is_exact(staged, projected):
        return
    raise ConfigValidationError(
        "projected enforced config is not the exact staged transition or pinned rollback target"
    )


def authorize_retirement(config, store, commit, review_id, reason, policy_id) -> None:
    """Authorize one retirement under the normalized staged policy."""
    if config.review_policy.mode is not ReviewPolicyMode.STAGED:
        raise ConfigValidationError("review retirement requires a staged policy")
    configured_ids = {
        exception.id for exception in config.review_policy.same_family_exceptions
    }
    if policy_id not in configured_ids:
        raise ConfigValidationError("retirement policy_id is not configured")
    source = store.review_path(commit, review_id)
    raw, _digest = store._read_bound_bytes(source, "active review record")
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
        if reason != expected_reason:
            raise ConfigValidationError(
                "retirement reason does not apply to the legacy source reviewer"
            )
        return
    if schema_version == 2:
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
        return
    raise ConfigValidationError("retirement source schema_version is not migratable")
