from __future__ import annotations

import json
import re
import sys
from typing import Dict, List, Mapping, NamedTuple, Optional, Tuple

from pr_closure.model import Disposition, Severity, Verdict


class ReviewValidationError(ValueError):
    """Raised when a review record or model family violates the contract."""


END = "(?![\\s\\S])"
COMMIT_ID_PATTERN = "^[0-9a-f]{40}" + END
REPOSITORY_PATTERN = "^[^/\\s]+/[^/\\s]+" + END

NON_BLANK = {"minLength": 1, "pattern": "\\S"}

SEVERITIES = tuple(item.value for item in Severity)
DISPOSITIONS = tuple(item.value for item in Disposition)
VERDICTS = tuple(item.value for item in Verdict)

SEMANTIC_ASYMMETRIES = (
    "duplicate finding IDs cannot be forbidden here: JSON Schema has no uniqueness "
    "keyword over object properties, so the Python gate rejects duplicate ids and this "
    "schema deliberately declares none.",
    "unknown model family (e.g. wizard-9000) is rejected only by the Python resolver: "
    "model lineage is not representable as a JSON Schema pattern, so this schema "
    "accepts any non-blank spelling.",
    "third-party lookalike (claude-killer) is rejected only by the Python resolver: a "
    "product name attached to a vendor prefix is not expressible in this schema, so the "
    "schema accepts it.",
    "same family under different spellings is rejected only by the Python gate: JSON "
    "Schema cannot compare implementer_family with reviewer_family, so this schema "
    "accepts families that resolve to the same lineage.",
    "schema_version 1.0 is rejected only by the Python gate: JSON Schema's const treats "
    "the numeric literal 1.0 as equal to 1, while the Python gate requires an exact "
    "integer type.",
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


def _check_string_array(name: str, raw, min_items: int) -> Tuple[str, ...]:
    items = require_list(raw, name)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ReviewValidationError(f"{name} must be a list of non-empty strings")
    if len(items) < min_items:
        raise ReviewValidationError(f"{name} must contain at least {min_items} item(s)")
    return tuple(items)


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
        elif kind == "object_array":
            items = require_list(raw, name)
            parser = parsers.get(name)
            if parser is None:
                raise AssertionError(f"object_array field {name} needs a parser")
            values[name] = [parser(item) for item in items]
        elif kind == "follow_up":
            values[name] = raw
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
    if kind == "follow_up":
        schema = {"type": "integer"}
        if "minimum" in params:
            schema["minimum"] = params["minimum"]
        return schema
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
        + "; ".join(SEMANTIC_ASYMMETRIES)
    )


def render_schema() -> str:
    return json.dumps(json_schema(), indent=2) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render_schema())