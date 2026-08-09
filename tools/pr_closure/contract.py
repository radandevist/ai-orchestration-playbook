from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Tuple

from pr_closure.model import Disposition, ProjectConfig, Severity, Verdict


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
        "integer-valued JSON numbers such as schema_version 1.0, pr_number 42.0, or "
        "follow_up_issue 900.0 satisfy JSON Schema integer/const semantics, while the "
        "Python gate requires exact int values and rejects floats and booleans.",
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
    _f("tracking_projection", "nullable_text"),
    _f("ci_required_checks", "check_name_array"),
)

PROJECT_CONFIG_ALLOWED_KEYS = tuple(field.name for field in PROJECT_CONFIG_FIELDS)
PROJECT_CONFIG_REQUIRED_KEYS = tuple(
    field.name for field in PROJECT_CONFIG_FIELDS if field.name != "ci_required_checks"
)

CONFIG_SEMANTIC_ASYMMETRIES = (
    SemanticAsymmetry(
        "exact_integer_types",
        "integer-valued JSON numbers such as schema_version 1.0, infra_retry_budget 1.0, "
        "or heavy_job_limit 1.0 satisfy JSON Schema integer/const semantics, while the "
        "Python gate requires exact int values and rejects floats and booleans.",
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


def command_sequence_digest(sequence) -> str:
    """SHA-256 identity of an exact ordered ``(phase, command_digest)`` sequence.

    This is the immutable config identity that verification attempts bind
    (T6L-F2): a changed ordered command sequence yields a new identity, so
    attempts for different configurations coexist at the same commit and a
    stale identity can never be selected as current passing evidence.
    """
    payload = json.dumps(
        [[phase, digest] for phase, digest in sequence], sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
        else:
            raise AssertionError(f"unknown contract kind: {kind!r}")
    return values


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
    try:
        reject_unknown_keys(record, PROJECT_CONFIG_ALLOWED_KEYS, "project config")
        require_present(record, PROJECT_CONFIG_REQUIRED_KEYS, "project config")
        values = check(PROJECT_CONFIG_FIELDS, record)
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
        tracking_projection=values["tracking_projection"],
        ci_required_checks=values.get("ci_required_checks", ()),
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


def _comment() -> str:
    return (
        "Generated from tools/pr_closure/contract.py by "
        "PYTHONPATH=tools python3 -m pr_closure.contract - never hand-edit. "
        "Residual semantics JSON Schema cannot express are enforced in Python and are "
        "documented verbatim: "
        + "; ".join(
            f"{asymmetry.id}: {asymmetry.description}"
            for asymmetry in SEMANTIC_ASYMMETRIES
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
    }


def render_schema() -> str:
    return json.dumps(json_schema(), indent=2) + "\n"


def render_project_schema() -> str:
    return json.dumps(project_json_schema(), indent=2) + "\n"


if __name__ == "__main__":
    project_schema_path = (
        Path(__file__).resolve().parent.parent / "schemas" / "project-closure-v1.json"
    )
    project_schema_path.write_text(render_project_schema())
    sys.stdout.write(render_schema())
