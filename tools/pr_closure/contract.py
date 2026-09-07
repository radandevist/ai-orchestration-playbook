from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Tuple

from pr_closure.model import (
    Disposition,
    ModelRoute,
    ProjectConfig,
    ReviewPolicy,
    ReviewPolicyMode,
    SameFamilyReviewException,
    Severity,
    Verdict,
)


class ReviewValidationError(ValueError):
    """Raised when a review record or model family violates the contract."""


class ConfigValidationError(ValueError):
    """Raised when a project closure configuration violates the contract."""


END = "(?![\\s\\S])"
COMMIT_ID_PATTERN = "^[0-9a-f]{40}" + END
COMMAND_DIGEST_PATTERN = "^[0-9a-f]{64}" + END
REPOSITORY_PATTERN = "^[^/\\s]+/[^/\\s]+" + END

NON_BLANK = {"minLength": 1, "pattern": "\\S"}

SEVERITIES = tuple(item.value for item in Severity)
DISPOSITIONS = tuple(item.value for item in Disposition)
VERDICTS = tuple(item.value for item in Verdict)

class SemanticAsymmetry(NamedTuple):
    id: str
    description: str


SEMANTIC_ASYMMETRIES = (
    SemanticAsymmetry(
        "duplicate_finding_ids",
        "duplicate finding IDs cannot be forbidden here: JSON Schema has no uniqueness "
        "keyword over object properties, so the Python gate rejects duplicate ids and "
        "this schema deliberately declares none.",
    ),
    SemanticAsymmetry(
        "unknown_model_family",
        "unknown model family (e.g. wizard-9000) is rejected only by the Python resolver: "
        "model lineage is not representable as a JSON Schema pattern, so this schema "
        "accepts any non-blank spelling.",
    ),
    SemanticAsymmetry(
        "third_party_lookalike",
        "third-party lookalike (claude-killer) is rejected only by the Python resolver: "
        "a product name attached to a vendor prefix is not expressible in this schema, "
        "so the schema accepts it.",
    ),
    SemanticAsymmetry(
        "same_model_family",
        "same family under different spellings is rejected only by the Python gate: JSON "
        "Schema cannot compare implementer_family with reviewer_family, so this schema "
        "accepts families that resolve to the same lineage.",
    ),
    SemanticAsymmetry(
        "exact_integer_types",
        "integer-valued JSON numbers such as schema_version 1.0, pr_number 42.0, "
        "or follow_up_issue 900.0 satisfy JSON Schema integer/const semantics, "
        "while the Python gate requires exact int values and rejects floats and booleans.",
    ),
)

EXACTLY_ONE_OF = ("base_commit", "comparison_range")
PAIRED_PROOF = ("bad_case_evidence", "good_case_evidence")
FOLLOW_UP_DISPOSITION = Disposition.FOLLOW_UP_ISSUE
FOLLOW_UP_FIELD = "follow_up_issue"


class _Field(NamedTuple):
    name: str
    kind: str
    params: Dict


def _f(name: str, kind: str, params: Optional[Dict] = None) -> _Field:
    return _Field(name, kind, params or {})


# Non-durable session areas. Config roots living here (or anywhere beneath
# them, including through symlinks) are rejected by Python; JSON Schema can
# only express the leading-slash requirement. Must stay consistent with
# ``pr_closure.store._FORBIDDEN_ROOT_SPECS``; the differential suite pins the
# equality, and ``store.py`` cannot import this module (it already imports
# ``COMMIT_ID_PATTERN`` from here, which would be a cycle).
CONFIG_FORBIDDEN_ROOT_SPECS = ("/tmp", os.path.expanduser("~/.claude/jobs"))

_ANY_WHITESPACE_RE = re.compile(r"\s")


def _forbidden_config_roots() -> Tuple[str, ...]:
    return tuple(os.path.realpath(os.path.abspath(spec)) for spec in CONFIG_FORBIDDEN_ROOT_SPECS)


def _is_forbidden_path(resolved: str) -> bool:
    for root in _forbidden_config_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


PROJECT_CONFIG_FIELDS = (
    _f("schema_version", "const_int", {"value": 1}),
    _f("project", "project_component"),
    _f("repository", "pattern", {"pattern": REPOSITORY_PATTERN}),
    _f("repo_path", "durable_path"),
    _f("default_branch", "branch"),
    _f("closure_state_dir", "durable_path"),
    _f("local_review_ready_commands", "command_array", {"min_items": 1}),
    _f("closure_acceptance_commands", "command_array", {"min_items": 1}),
    _f("infra_retry_budget", "integer", {"minimum": 1}),
    _f("stagnation_budget_minutes", "integer", {"minimum": 1}),
    _f("heavy_job_limit", "const_int", {"value": 1}),
    _f("verification_command_timeout_seconds", "integer", {"minimum": 1}),
    _f("tracking_projection", "nullable_text"),
    _f("ci_required_checks", "check_name_array"),
    _f("model_routes", "policy_routes"),
    _f("review_policy", "review_policy"),
)

PROJECT_CONFIG_ALLOWED_KEYS = tuple(field.name for field in PROJECT_CONFIG_FIELDS)
PROJECT_CONFIG_REQUIRED_KEYS = tuple(
    field.name
    for field in PROJECT_CONFIG_FIELDS
    if field.name not in ("ci_required_checks", "model_routes", "review_policy")
)

CONFIG_SEMANTIC_ASYMMETRIES = (
    SemanticAsymmetry(
        "exact_integer_types",
        "integer-valued JSON numbers such as schema_version 1.0, infra_retry_budget 1.0, "
        "heavy_job_limit 1.0, or verification_command_timeout_seconds 1.0 satisfy JSON Schema "
        "integer/const semantics, while the Python gate requires exact int values and rejects "
        "floats and booleans.",
    ),
    SemanticAsymmetry(
        "forbidden_temporary_paths",
        "repo_path and closure_state_dir must resolve to durable absolute paths outside "
        "/tmp and the session job area; JSON Schema can only express the leading-slash "
        "requirement, so this schema accepts /tmp roots the Python gate rejects.",
    ),
    SemanticAsymmetry(
        "unsafe_project_component",
        "the project name must be a single safe path component (no slash, backslash, NUL, "
        "or surrounding whitespace, and not '.' or '..'); JSON Schema cannot exclude "
        "'.'/'..'/NUL, so this schema accepts spellings the Python gate rejects.",
    ),
    SemanticAsymmetry(
        "nul_command_string",
        "configured command strings must not contain NUL bytes because the subprocess "
        "argv contract cannot carry them; JSON Schema's \\S pattern accepts NUL, so "
        "this schema accepts spellings the Python gate rejects.",
    ),
    SemanticAsymmetry(
        "project_model_registry",
        "canonical model and launcher-registry membership for project routes is a Python "
        "registry lookup; JSON Schema can constrain exact spelling shape but cannot reject "
        "an unknown model or endpoint identity.",
    ),
    SemanticAsymmetry(
        "project_route_policy",
        "project route uniqueness, same-family exception concordance, forbidden reviewer "
        "families, and staged/enforced cross-object policy relations are Python checks; "
        "JSON Schema cannot compare those normalized values.",
    ),
)

V2_SEMANTIC_ASYMMETRIES = (
    SemanticAsymmetry(
        "canonical_model_registry",
        "canonical model and family resolution is a Python registry lookup; JSON Schema can "
        "only constrain the spelling shape and cannot reject unknown model IDs or family-only "
        "labels.",
    ),
    SemanticAsymmetry(
        "launcher_provenance_membership",
        "launcher membership, immutable run-manifest identity, producer-output existence, and "
        "independent output digest verification are Python checks; JSON Schema cannot read "
        "registries or the closure filesystem.",
    ),
    SemanticAsymmetry(
        "project_route_policy",
        "project model-route selection, forbidden reviewer families, and configured exception "
        "policy IDs are normalized ProjectConfig checks; JSON Schema cannot compare those "
        "cross-object values.",
    ),
    SemanticAsymmetry(
        "duplicate_json_keys",
        "duplicate JSON object keys are rejected by the strict importer before durable storage; "
        "JSON Schema validates the decoded object after a parser has already collapsed keys.",
    ),
)


RECORD_FIELDS = (
    _f("schema_version", "const_int", {"value": 1}),
    _f("repository", "pattern", {"pattern": REPOSITORY_PATTERN}),
    _f("pr_number", "integer", {"minimum": 1}),
    _f("reviewed_branch", "text"),
    _f("reviewed_commit", "pattern", {"pattern": COMMIT_ID_PATTERN, "length": 40}),
    _f("base_commit", "pattern", {"pattern": COMMIT_ID_PATTERN, "length": 40}),
    _f("comparison_range", "text"),
    _f("implementer_family", "text"),
    _f("reviewer_family", "text"),
    _f("local_evidence", "string_array", {"min_items": 1}),
    _f("ci_evidence", "string_array", {"min_items": 1}),
    _f("verdict", "enum", {"values": VERDICTS}),
    _f("findings", "object_array", {"ref": "finding"}),
    _f("intentionally_not_findings", "string_array", {"min_items": 0}),
)

FINDING_FIELDS = (
    _f("id", "text"),
    _f("root_cause", "text"),
    _f("severity", "enum", {"values": SEVERITIES}),
    _f("disposition", "enum", {"values": DISPOSITIONS}),
    _f("scope", "text"),
    _f("summary", "text"),
    _f("evidence", "string_array", {"min_items": 0}),
    _f("follow_up_issue", "follow_up", {"minimum": 1}),
    _f("bad_case_evidence", "string_array", {"min_items": 1}),
    _f("good_case_evidence", "string_array", {"min_items": 1}),
)

ALLOWED_TOP_LEVEL_KEYS = tuple(field.name for field in RECORD_FIELDS)
REQUIRED_TOP_LEVEL_KEYS = tuple(
    field.name for field in RECORD_FIELDS if field.name not in EXACTLY_ONE_OF
)
ALLOWED_FINDING_KEYS = tuple(field.name for field in FINDING_FIELDS)
REQUIRED_FINDING_KEYS = tuple(
    field.name
    for field in FINDING_FIELDS
    if field.name not in PAIRED_PROOF and field.name != FOLLOW_UP_FIELD
)


def require_mapping(value, context: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ReviewValidationError(f"{context} is not a mapping/object")
    return value


def require_list(value, context: str) -> list:
    if not isinstance(value, list):
        raise ReviewValidationError(f"{context} is not a list")
    return value


def reject_unknown_keys(mapping, allowed, context: str) -> None:
    unknown = [key for key in mapping if key not in allowed]
    if unknown:
        raise ReviewValidationError(
            f"unknown {context} key(s): {', '.join(sorted(unknown))}"
        )


def require_present(mapping, keys, context: str) -> None:
    missing = [key for key in keys if key not in mapping]
    if missing:
        raise ReviewValidationError(f"missing {context} field(s): {', '.join(missing)}")


def _check_text(name: str, raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ReviewValidationError(f"{name} must be a non-empty string")
    return raw


def _check_pattern(name: str, raw, pattern: str) -> str:
    if not isinstance(raw, str) or re.compile(pattern).fullmatch(raw) is None:
        raise ReviewValidationError(f"{name} must match {pattern}")
    return raw


def _check_integer(name: str, raw, minimum: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < minimum:
        raise ReviewValidationError(f"{name} must be an integer at least {minimum}")
    return raw


def _check_const_int(name: str, raw, value: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw != value:
        raise ReviewValidationError(f"unsupported {name}: {raw!r}")
    return raw


def _check_enum(name: str, raw, values) -> str:
    if not isinstance(raw, str) or raw not in values:
        raise ReviewValidationError(f"unsupported {name}: {raw!r}")
    return raw


def command_digest(command: str) -> str:
    """SHA-256 content digest of a configured command string.

    Durable evidence binds what ran through this digest and never persists the
    raw command text (C6D-F4); the digest is the comparison key for
    config-change staleness (C6D-F2).
    """
    if not isinstance(command, str):
        raise ReviewValidationError("command_digest requires a string command")
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def legacy_command_sequence_digest(sequence) -> str:
    """Legacy SHA-256 identity of an ordered ``(phase, command_digest)`` list.

    This is the pre-timeout binding format retained for explicit
    compatibility checks when reading historical verification artifacts that did
    not persist ``verification_command_timeout_seconds``.
    """
    payload = json.dumps(
        [[phase, digest] for phase, digest in sequence],
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def command_sequence_digest(sequence, verification_command_timeout_seconds: int = 300) -> str:
    """SHA-256 identity of an exact ordered command identity sequence.

    The digest binds both the ordered command sequence and the configured
    verification timeout (T6L-F2): a changed command sequence or timeout yields
    a new identity, so attempts for different configurations coexist at the
    same commit and a stale identity can never be selected as current passing
    evidence.
    """
    payload = json.dumps(
        {
            "verification_command_timeout_seconds": verification_command_timeout_seconds,
            "commands": [[phase, digest] for phase, digest in sequence],
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def configuration_digest(record: Mapping) -> str:
    """Return the stable digest used to bind policy activation evidence."""
    try:
        payload = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError) as error:
        raise ConfigValidationError("configuration cannot be canonically serialized") from error
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_string_array(name: str, raw, min_items: int) -> Tuple[str, ...]:
    items = require_list(raw, name)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ReviewValidationError(f"{name} must be a list of non-empty strings")
    if len(items) < min_items:
        raise ReviewValidationError(f"{name} must contain at least {min_items} item(s)")
    return tuple(items)


def _check_check_name_array(name: str, raw) -> Tuple[str, ...]:
    """Validate an optional authoritative CI-check-name list.

    Names are exact, non-blank, and unique. An absent or empty list keeps the
    strict all-rollup classification; a typo fails closed at selection time as
    a missing required check rather than silently widening the gate.
    """
    items = require_list(raw, name)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ReviewValidationError(f"{name} must be a list of non-empty check names")
    seen = set()
    for item in items:
        if item in seen:
            raise ReviewValidationError(f"{name} must not contain duplicate check names")
        seen.add(item)
    return tuple(items)


def _check_project_component(name: str, raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ReviewValidationError(f"{name} must be a non-empty string")
    if raw != raw.strip():
        raise ReviewValidationError(f"{name} must not have surrounding whitespace")
    if raw in (".", ".."):
        raise ReviewValidationError(f"{name} must not be '.' or '..'")
    if any(char in raw for char in ("/", "\\", "\x00")):
        raise ReviewValidationError(f"{name} must be a single safe path component")
    return raw


def _check_durable_path(name: str, raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ReviewValidationError(f"{name} must be a non-empty absolute path")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise ReviewValidationError(f"{name} must be an absolute path")
    resolved = os.path.realpath(expanded)
    if _is_forbidden_path(resolved):
        raise ReviewValidationError(
            f"{name} must not live under a temporary session area: {resolved}"
        )
    return resolved


def _check_branch(name: str, raw) -> str:
    if not isinstance(raw, str) or not raw.strip() or _ANY_WHITESPACE_RE.search(raw):
        raise ReviewValidationError(f"{name} must be a non-empty string without whitespace")
    return raw


def _check_command_array(name: str, raw, min_items: int) -> Tuple[str, ...]:
    items = require_list(raw, name)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ReviewValidationError(f"{name} must be a list of non-empty command strings")
    if any(isinstance(item, str) and "\x00" in item for item in items):
        raise ReviewValidationError(
            f"{name} commands must not contain NUL bytes (subprocess cannot run them)"
        )
    if len(items) < min_items:
        raise ReviewValidationError(f"{name} must contain at least {min_items} item(s)")
    return tuple(items)


def _check_nullable_text(name: str, raw) -> Optional[str]:
    if raw is None:
        return None
    return _check_text(name, raw)


_SAFE_POLICY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_POLICY_KEYS = frozenset({
    "mode",
    "owner_authorization",
    "forbidden_reviewer_families",
    "same_family_exceptions",
})
_EXCEPTION_KEYS = frozenset({
    "id",
    "registry_version",
    "implementer_family",
    "reviewer_model",
    "required_for_authorized_family",
    "owner_authorization",
    "rationale",
})
_ROUTE_KEYS = frozenset({
    "id",
    "registry_version",
    "launcher_registry_version",
    "implementer_model",
    "implementer_runner",
    "implementer_invocation_model",
    "reviewer_model",
    "reviewer_runner",
    "reviewer_invocation_model",
    "same_family_policy_id",
})


def _policy_id(name: str, raw) -> str:
    value = _check_text(name, raw)
    if _SAFE_POLICY_ID.fullmatch(value) is None:
        raise ReviewValidationError(f"{name} must be a path-safe lowercase identifier")
    return value


def _known_family(raw) -> str:
    from pr_closure import families

    return families.resolve_family(_check_text("model family", raw))


def _parse_same_family_exception(raw) -> SameFamilyReviewException:
    from pr_closure.registries import RegistryValidationError, require_model

    item = require_mapping(raw, "same-family exception")
    reject_unknown_keys(item, _EXCEPTION_KEYS, "same-family exception")
    require_present(item, _EXCEPTION_KEYS, "same-family exception")
    exception_id = _policy_id("same-family exception id", item["id"])
    registry_version = _check_text("registry_version", item["registry_version"])
    implementer_family = _known_family(item["implementer_family"])
    reviewer_model = _check_text("reviewer_model", item["reviewer_model"])
    try:
        reviewer_family = require_model(registry_version, reviewer_model)
    except RegistryValidationError as error:
        raise ReviewValidationError(str(error)) from error
    if reviewer_family != implementer_family:
        raise ReviewValidationError(
            "same-family exception implementer and reviewer families must match"
        )
    required = item["required_for_authorized_family"]
    if not isinstance(required, bool):
        raise ReviewValidationError("required_for_authorized_family must be a boolean")
    return SameFamilyReviewException(
        id=exception_id,
        registry_version=registry_version,
        implementer_family=implementer_family,
        reviewer_model=reviewer_model,
        required_for_authorized_family=required,
        owner_authorization=_check_text(
            "same-family exception owner_authorization", item["owner_authorization"]
        ),
        rationale=_check_text("same-family exception rationale", item["rationale"]),
    )


def _parse_review_policy(raw) -> ReviewPolicy:
    item = require_mapping(raw, "review_policy")
    reject_unknown_keys(item, _POLICY_KEYS, "review_policy")
    mode_raw = item.get("mode")
    owner_raw = item.get("owner_authorization")
    forbidden_raw = item.get("forbidden_reviewer_families", [])
    exceptions_raw = item.get("same_family_exceptions", [])

    forbidden_items = require_list(forbidden_raw, "forbidden_reviewer_families")
    forbidden = tuple(_known_family(value) for value in forbidden_items)
    if len(set(forbidden)) != len(forbidden):
        raise ReviewValidationError("forbidden_reviewer_families must be unique")
    exception_items = require_list(exceptions_raw, "same_family_exceptions")
    exceptions = tuple(_parse_same_family_exception(value) for value in exception_items)
    exception_ids = [value.id for value in exceptions]
    if len(set(exception_ids)) != len(exception_ids):
        raise ReviewValidationError("same-family exception ids must be unique")

    has_content = (
        mode_raw is not None
        or owner_raw is not None
        or len(forbidden) > 0
        or len(exceptions) > 0
    )
    if not has_content:
        return ReviewPolicy()
    if not isinstance(mode_raw, str) or mode_raw not in tuple(ReviewPolicyMode):
        raise ReviewValidationError("review_policy mode must be staged or enforced")
    owner = _check_text("review_policy owner_authorization", owner_raw)
    return ReviewPolicy(
        mode=ReviewPolicyMode(mode_raw),
        owner_authorization=owner,
        forbidden_reviewer_families=forbidden,
        same_family_exceptions=exceptions,
    )


def _parse_model_route(raw) -> ModelRoute:
    from pr_closure.registries import RegistryValidationError, require_launcher, require_model

    item = require_mapping(raw, "model route")
    reject_unknown_keys(item, _ROUTE_KEYS, "model route")
    require_present(item, _ROUTE_KEYS, "model route")
    registry_version = _check_text("registry_version", item["registry_version"])
    launcher_version = _check_text(
        "launcher_registry_version", item["launcher_registry_version"]
    )
    implementer_model = _check_text("implementer_model", item["implementer_model"])
    reviewer_model = _check_text("reviewer_model", item["reviewer_model"])
    implementer_runner = _check_text("implementer_runner", item["implementer_runner"])
    implementer_invocation = _check_text(
        "implementer_invocation_model", item["implementer_invocation_model"]
    )
    reviewer_runner = _check_text("reviewer_runner", item["reviewer_runner"])
    reviewer_invocation = _check_text(
        "reviewer_invocation_model", item["reviewer_invocation_model"]
    )
    try:
        require_model(registry_version, implementer_model)
        require_model(registry_version, reviewer_model)
        require_launcher(
            registry_version,
            launcher_version,
            implementer_model,
            implementer_runner,
            implementer_invocation,
        )
        require_launcher(
            registry_version,
            launcher_version,
            reviewer_model,
            reviewer_runner,
            reviewer_invocation,
        )
    except RegistryValidationError as error:
        raise ReviewValidationError(str(error)) from error
    policy_id_raw = item["same_family_policy_id"]
    policy_id = None
    if policy_id_raw is not None:
        policy_id = _policy_id("same_family_policy_id", policy_id_raw)
    return ModelRoute(
        id=_policy_id("model route id", item["id"]),
        registry_version=registry_version,
        launcher_registry_version=launcher_version,
        implementer_model=implementer_model,
        implementer_runner=implementer_runner,
        implementer_invocation_model=implementer_invocation,
        reviewer_model=reviewer_model,
        reviewer_runner=reviewer_runner,
        reviewer_invocation_model=reviewer_invocation,
        same_family_policy_id=policy_id,
    )


def _parse_model_routes(raw) -> Tuple[ModelRoute, ...]:
    return tuple(_parse_model_route(value) for value in require_list(raw, "model_routes"))


def check(fields, record: Mapping, parsers: Optional[Dict] = None) -> Dict:
    """Validate every declared field present in ``record``.

    Presence rules live in ``require_present``; this validates the shape of each field
    that is present and returns the normalized Python value. ``parsers`` maps an
    ``object_array`` field name to the callable that validates each element.
    """
    parsers = parsers or {}
    values = {}
    for name, kind, params in fields:
        if name not in record:
            continue
        raw = record[name]
        if kind == "text":
            values[name] = _check_text(name, raw)
        elif kind == "pattern":
            values[name] = _check_pattern(name, raw, params["pattern"])
        elif kind == "integer":
            values[name] = _check_integer(name, raw, params.get("minimum", 0))
        elif kind == "const_int":
            values[name] = _check_const_int(name, raw, params["value"])
        elif kind == "enum":
            values[name] = _check_enum(name, raw, params["values"])
        elif kind == "string_array":
            values[name] = _check_string_array(name, raw, params.get("min_items", 0))
        elif kind == "check_name_array":
            values[name] = _check_check_name_array(name, raw)
        elif kind == "object_array":
            items = require_list(raw, name)
            parser = parsers.get(name)
            if parser is None:
                raise AssertionError(f"object_array field {name} needs a parser")
            values[name] = [parser(item) for item in items]
        elif kind == "follow_up":
            values[name] = raw
        elif kind == "project_component":
            values[name] = _check_project_component(name, raw)
        elif kind == "durable_path":
            values[name] = _check_durable_path(name, raw)
        elif kind == "branch":
            values[name] = _check_branch(name, raw)
        elif kind == "command_array":
            values[name] = _check_command_array(name, raw, params.get("min_items", 0))
        elif kind == "nullable_text":
            values[name] = _check_nullable_text(name, raw)
        elif kind == "policy_routes":
            values[name] = _parse_model_routes(raw)
        elif kind == "review_policy":
            values[name] = _parse_review_policy(raw)
        else:
            raise AssertionError(f"unknown contract kind: {kind!r}")
    return values


def _validate_policy_routes(policy: ReviewPolicy, routes: Tuple[ModelRoute, ...]) -> None:
    from pr_closure.registries import require_model

    if policy.mode is None:
        if len(routes) > 0:
            raise ReviewValidationError("a disabled review_policy requires empty model_routes")
        return
    if len(routes) == 0:
        raise ReviewValidationError("an active review_policy requires non-empty model_routes")

    route_ids = [route.id for route in routes]
    implementer_models = [route.implementer_model for route in routes]
    if len(set(route_ids)) != len(route_ids):
        raise ReviewValidationError("model route ids must be unique")
    if len(set(implementer_models)) != len(implementer_models):
        raise ReviewValidationError("model route implementer models must be unique")

    exceptions = {exception.id: exception for exception in policy.same_family_exceptions}
    for exception in policy.same_family_exceptions:
        reviewer_family = require_model(exception.registry_version, exception.reviewer_model)
        if reviewer_family in policy.forbidden_reviewer_families:
            raise ReviewValidationError(
                "same-family exception reviewer family is forbidden: {0}".format(
                    reviewer_family
                )
            )

    for route in routes:
        implementer_family = require_model(route.registry_version, route.implementer_model)
        reviewer_family = require_model(route.registry_version, route.reviewer_model)
        if route.implementer_model == route.reviewer_model:
            raise ReviewValidationError("a model route cannot authorize model self-review")
        if reviewer_family in policy.forbidden_reviewer_families:
            raise ReviewValidationError(
                "model route reviewer family is forbidden: {0}".format(reviewer_family)
            )
        if implementer_family == reviewer_family:
            exception = exceptions.get(route.same_family_policy_id)
            if exception is None:
                raise ReviewValidationError(
                    "same-family model route requires a configured exception"
                )
            if (
                exception.registry_version != route.registry_version
                or exception.implementer_family != implementer_family
                or exception.reviewer_model != route.reviewer_model
            ):
                raise ReviewValidationError(
                    "same-family model route does not match its configured exception"
                )
        elif route.same_family_policy_id is not None:
            raise ReviewValidationError(
                "cross-family model route must not claim a same-family exception"
            )

    for exception in policy.same_family_exceptions:
        if not exception.required_for_authorized_family:
            continue
        for route in routes:
            implementer_family = require_model(
                route.registry_version, route.implementer_model
            )
            if implementer_family != exception.implementer_family:
                continue
            if (
                route.registry_version != exception.registry_version
                or route.reviewer_model != exception.reviewer_model
                or route.same_family_policy_id != exception.id
            ):
                raise ReviewValidationError(
                    "required same-family exception is missing from an authorized family route"
                )


def check_follow_up(record, disposition) -> Optional[int]:
    has_follow_up = FOLLOW_UP_FIELD in record
    if disposition == FOLLOW_UP_DISPOSITION:
        if not has_follow_up:
            raise ReviewValidationError(
                "FOLLOW_UP_ISSUE finding requires a follow_up_issue number"
            )
        return _check_integer(FOLLOW_UP_FIELD, record[FOLLOW_UP_FIELD], 1)
    if has_follow_up:
        raise ReviewValidationError(
            "follow_up_issue only applies to FOLLOW_UP_ISSUE findings"
        )
    return None


def validate_project_config(record: Mapping) -> ProjectConfig:
    """Validate a version-1 project closure configuration.

    Rejects unknown or missing keys, booleans/floats where integers are
    required, unsupported schema versions, unsafe project/repository/branch
    values, relative or forbidden temporary/session paths, empty command
    lists, empty/whitespace/non-string commands, non-positive budgets, and
    heavy-job limits other than the exact exclusive limit of 1. Path values
    are resolved before they are returned; no filesystem state is created.
    """
    record = require_mapping(record, "project config")
    config_digest = configuration_digest(record)
    staged_config_digest = None
    raw_policy = record.get("review_policy")
    if isinstance(raw_policy, Mapping) and raw_policy.get("mode") in (
        ReviewPolicyMode.STAGED.value,
        ReviewPolicyMode.ENFORCED.value,
    ):
        staged_record = dict(record)
        staged_policy = dict(raw_policy)
        staged_policy["mode"] = ReviewPolicyMode.STAGED.value
        staged_record["review_policy"] = staged_policy
        staged_config_digest = configuration_digest(staged_record)
    try:
        reject_unknown_keys(record, PROJECT_CONFIG_ALLOWED_KEYS, "project config")
        require_present(record, PROJECT_CONFIG_REQUIRED_KEYS, "project config")
        values = check(PROJECT_CONFIG_FIELDS, record)
        review_policy = values.get("review_policy", ReviewPolicy())
        model_routes = values.get("model_routes", ())
        _validate_policy_routes(review_policy, model_routes)
        from pr_closure.registries import RegistryValidationError, require_policy_floor

        try:
            require_policy_floor(record["repository"], review_policy, model_routes)
        except RegistryValidationError as error:
            raise ReviewValidationError(str(error)) from error
    except ReviewValidationError as error:
        raise ConfigValidationError(str(error)) from error
    return ProjectConfig(
        schema_version=values["schema_version"],
        project=values["project"],
        repository=values["repository"],
        repo_path=values["repo_path"],
        default_branch=values["default_branch"],
        closure_state_dir=values["closure_state_dir"],
        local_review_ready_commands=values["local_review_ready_commands"],
        closure_acceptance_commands=values["closure_acceptance_commands"],
        infra_retry_budget=values["infra_retry_budget"],
        stagnation_budget_minutes=values["stagnation_budget_minutes"],
        heavy_job_limit=values["heavy_job_limit"],
        verification_command_timeout_seconds=values["verification_command_timeout_seconds"],
        tracking_projection=values["tracking_projection"],
        ci_required_checks=values.get("ci_required_checks", ()),
        model_routes=model_routes,
        review_policy=review_policy,
        config_digest=config_digest,
        staged_config_digest=staged_config_digest,
    )


def _property_schema(name: str, kind: str, params: Dict) -> Dict:
    if kind == "text":
        return dict({"type": "string"}, **NON_BLANK)
    if kind == "pattern":
        schema = {"type": "string", "pattern": params["pattern"]}
        if "length" in params:
            schema["minLength"] = params["length"]
            schema["maxLength"] = params["length"]
        return schema
    if kind == "integer":
        schema = {"type": "integer"}
        if "minimum" in params:
            schema["minimum"] = params["minimum"]
        return schema
    if kind == "const_int":
        return {"type": "integer", "const": params["value"]}
    if kind == "enum":
        return {"type": "string", "enum": list(params["values"])}
    if kind in ("string_array", "object_array"):
        schema = {"type": "array"}
        if kind == "string_array":
            schema["items"] = dict({"type": "string"}, **NON_BLANK)
            if params.get("min_items", 0) > 0:
                schema["minItems"] = params["min_items"]
        else:
            schema["items"] = {"$ref": f"#/$defs/{params['ref']}"}
        return schema
    if kind == "check_name_array":
        return {
            "type": "array",
            "items": dict({"type": "string"}, **NON_BLANK),
            "uniqueItems": True,
        }
    if kind == "follow_up":
        schema = {"type": "integer"}
        if "minimum" in params:
            schema["minimum"] = params["minimum"]
        return schema
    if kind == "project_component":
        return {"type": "string", "pattern": "^[^/\\\\\\s]+$"}
    if kind == "durable_path":
        return {"type": "string", "minLength": 1, "pattern": "^/\\S"}
    if kind == "branch":
        return {"type": "string", "pattern": "^\\S+$"}
    if kind == "command_array":
        schema = {"type": "array", "items": dict({"type": "string"}, **NON_BLANK)}
        if params.get("min_items", 0) > 0:
            schema["minItems"] = params["min_items"]
        return schema
    if kind == "nullable_text":
        return {"type": ["string", "null"], **NON_BLANK}
    if kind == "policy_routes":
        return {"type": "array", "items": {"$ref": "#/$defs/modelRoute"}}
    if kind == "review_policy":
        return {"$ref": "#/$defs/reviewPolicy"}
    raise AssertionError(f"unknown contract kind: {kind!r}")


def json_schema() -> Dict:
    finding_def = {
        "type": "object",
        "required": list(REQUIRED_FINDING_KEYS),
        "additionalProperties": False,
        "dependentRequired": {
            PAIRED_PROOF[0]: [PAIRED_PROOF[1]],
            PAIRED_PROOF[1]: [PAIRED_PROOF[0]],
        },
        "properties": {field.name: _property_schema(*field) for field in FINDING_FIELDS},
        "if": {
            "properties": {"disposition": {"const": FOLLOW_UP_DISPOSITION.value}},
            "required": ["disposition"],
        },
        "then": {"required": [FOLLOW_UP_FIELD]},
        "else": {"not": {"required": [FOLLOW_UP_FIELD]}},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://ai-orchestration-playbook/schemas/review-record-v1.json",
        "title": "Structured Adversarial Review Record",
        "description": (
            "Machine authority for an independent cross-family PR review. The gate "
            "promotes mandatory scopes to BLOCKS_PR independently of reviewer labels."
        ),
        "$comment": _comment(),
        "type": "object",
        "required": list(REQUIRED_TOP_LEVEL_KEYS),
        "additionalProperties": False,
        "properties": {field.name: _property_schema(*field) for field in RECORD_FIELDS},
        "oneOf": [
            {"required": [EXACTLY_ONE_OF[0]]},
            {"required": [EXACTLY_ONE_OF[1]]},
        ],
        "$defs": {"finding": finding_def},
    }


def review_json_schema_v2() -> Dict:
    schema = copy.deepcopy(json_schema())
    model_id_pattern = "^[a-z0-9][a-z0-9.-]*" + END
    schema["$comment"] = _comment(v2=True)
    schema["$id"] = "https://ai-orchestration-playbook/schemas/review-record-v2.json"
    schema["title"] = "Structured Exact-Model Adversarial Review Record"
    schema["description"] = (
        "Machine authority for a registry-pinned review with both implementer "
        "and reviewer provenance. Policy and family comparisons are enforced in Python."
    )
    schema["properties"]["schema_version"] = {"type": "integer", "const": 2}
    schema["properties"].update({
        "implementer_model": {
            "type": "string",
            "pattern": model_id_pattern,
        },
        "reviewer_model": {
            "type": "string",
            "pattern": model_id_pattern,
        },
        "review_exception": {"$ref": "#/$defs/reviewException"},
        "provenance": {"$ref": "#/$defs/provenance"},
    })
    schema["required"].extend([
        "implementer_model",
        "reviewer_model",
        "provenance",
    ])
    participant = {
        "type": "object",
        "required": sorted((
            "model_id",
            "runner",
            "invocation_model",
            "run_ref",
            "durable_path",
            "sha256",
        )),
        "additionalProperties": False,
        "properties": {
            "model_id": {"type": "string", "pattern": model_id_pattern},
            "runner": dict({"type": "string"}, **NON_BLANK),
            "invocation_model": dict({"type": "string"}, **NON_BLANK),
            "run_ref": dict({"type": "string"}, **NON_BLANK),
            "durable_path": {"type": "string", "pattern": "^/\\S"},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    }
    schema["$defs"].update({
        "reviewException": {
            "type": "object",
            "required": ["policy_id"],
            "additionalProperties": False,
            "properties": {
                "policy_id": dict({"type": "string"}, **NON_BLANK),
            },
        },
        "provenance": {
            "type": "object",
            "required": [
                "registry_version",
                "launcher_registry_version",
                "implementer",
                "reviewer",
            ],
            "additionalProperties": False,
            "properties": {
                "registry_version": dict({"type": "string"}, **NON_BLANK),
                "launcher_registry_version": dict({"type": "string"}, **NON_BLANK),
                "implementer": {"$ref": "#/$defs/provenanceParticipant"},
                "reviewer": {"$ref": "#/$defs/provenanceParticipant"},
            },
        },
        "provenanceParticipant": participant,
    })
    return schema


def _comment(v2: bool = False) -> str:
    asymmetries = SEMANTIC_ASYMMETRIES + (V2_SEMANTIC_ASYMMETRIES if v2 else ())
    return (
        "Generated from tools/pr_closure/contract.py by "
        "PYTHONPATH=tools python3 -m pr_closure.contract - never hand-edit. "
        "Residual semantics JSON Schema cannot express are enforced in Python and are "
        "documented verbatim: "
        + "; ".join(
            f"{asymmetry.id}: {asymmetry.description}"
            for asymmetry in asymmetries
        )
    )


def _project_comment() -> str:
    return (
        "Generated from tools/pr_closure/contract.py by "
        "PYTHONPATH=tools python3 -m pr_closure.contract - never hand-edit. "
        "Residual semantics JSON Schema cannot express are enforced in Python and are "
        "documented verbatim: "
        + "; ".join(
            f"{asymmetry.id}: {asymmetry.description}"
            for asymmetry in CONFIG_SEMANTIC_ASYMMETRIES
        )
    )


def project_json_schema() -> Dict:
    policy_id = {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]*" + END}
    model_id = {"type": "string", "pattern": "^[a-z0-9][a-z0-9.-]*" + END}
    non_blank = dict({"type": "string"}, **NON_BLANK)
    exception_def = {
        "type": "object",
        "required": sorted(_EXCEPTION_KEYS),
        "additionalProperties": False,
        "properties": {
            "id": policy_id,
            "registry_version": non_blank,
            "implementer_family": non_blank,
            "reviewer_model": model_id,
            "required_for_authorized_family": {"type": "boolean"},
            "owner_authorization": non_blank,
            "rationale": non_blank,
        },
    }
    route_def = {
        "type": "object",
        "required": sorted(_ROUTE_KEYS),
        "additionalProperties": False,
        "properties": {
            "id": policy_id,
            "registry_version": non_blank,
            "launcher_registry_version": non_blank,
            "implementer_model": model_id,
            "implementer_runner": non_blank,
            "implementer_invocation_model": non_blank,
            "reviewer_model": model_id,
            "reviewer_runner": non_blank,
            "reviewer_invocation_model": non_blank,
            "same_family_policy_id": {"anyOf": [policy_id, {"type": "null"}]},
        },
    }
    review_policy_def = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mode": {
                "anyOf": [
                    {"type": "string", "enum": [item.value for item in ReviewPolicyMode]},
                    {"type": "null"},
                ]
            },
            "owner_authorization": {
                "anyOf": [non_blank, {"type": "null"}],
            },
            "forbidden_reviewer_families": {
                "type": "array",
                "items": non_blank,
                "uniqueItems": True,
            },
            "same_family_exceptions": {
                "type": "array",
                "items": {"$ref": "#/$defs/sameFamilyException"},
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://ai-orchestration-playbook/schemas/project-closure-v1.json",
        "title": "PR Closure Project Configuration",
        "description": (
            "Fail-closed versioned project configuration for the PR closure gate. "
            "Path safety and component rules JSON Schema cannot express are enforced "
            "in Python."
        ),
        "$comment": _project_comment(),
        "type": "object",
        "required": list(PROJECT_CONFIG_REQUIRED_KEYS),
        "additionalProperties": False,
        "properties": {field.name: _property_schema(*field) for field in PROJECT_CONFIG_FIELDS},
        "oneOf": [
            {
                "properties": {
                    "model_routes": {"maxItems": 0},
                    "review_policy": {
                        "properties": {
                            "mode": {"type": "null"},
                            "owner_authorization": {"type": "null"},
                            "forbidden_reviewer_families": {"maxItems": 0},
                            "same_family_exceptions": {"maxItems": 0},
                        }
                    },
                }
            },
            {
                "required": ["model_routes", "review_policy"],
                "properties": {
                    "model_routes": {"minItems": 1},
                    "review_policy": {
                        "required": ["mode", "owner_authorization"],
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": [item.value for item in ReviewPolicyMode],
                            },
                            "owner_authorization": non_blank,
                        },
                    },
                },
            },
        ],
        "$defs": {
            "modelRoute": route_def,
            "reviewPolicy": review_policy_def,
            "sameFamilyException": exception_def,
        },
    }


def render_schema() -> str:
    return json.dumps(json_schema(), indent=2) + "\n"


def render_project_schema() -> str:
    return json.dumps(project_json_schema(), indent=2) + "\n"


def render_review_schema_v2() -> str:
    return json.dumps(review_json_schema_v2(), indent=2) + "\n"


if __name__ == "__main__":
    project_schema_path = (
        Path(__file__).resolve().parent.parent / "schemas" / "project-closure-v1.json"
    )
    project_schema_path.write_text(render_project_schema())
    review_v2_schema_path = (
        Path(__file__).resolve().parent.parent / "schemas" / "review-record-v2.json"
    )
    review_v2_schema_path.write_text(render_review_schema_v2())
    sys.stdout.write(render_schema())
