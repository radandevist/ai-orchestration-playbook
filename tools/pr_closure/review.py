from __future__ import annotations

import re
from typing import List, Mapping, Optional, Tuple

from pr_closure.model import Disposition, Finding, ReviewRecord, Severity, Verdict

COMMIT_ID_RE = re.compile(r"[0-9a-f]{40}")
REPOSITORY_RE = re.compile(r"[^/]+/[^/]+")

TOP_LEVEL_FIELDS = (
    "schema_version",
    "repository",
    "pr_number",
    "reviewed_branch",
    "reviewed_commit",
    "implementer_family",
    "reviewer_family",
    "local_evidence",
    "ci_evidence",
    "verdict",
    "findings",
    "intentionally_not_findings",
    "base_commit",
    "comparison_range",
)

REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "repository",
    "pr_number",
    "reviewed_branch",
    "reviewed_commit",
    "implementer_family",
    "reviewer_family",
    "local_evidence",
    "ci_evidence",
    "verdict",
    "findings",
    "intentionally_not_findings",
)

FINDING_FIELDS = (
    "id",
    "root_cause",
    "severity",
    "disposition",
    "scope",
    "summary",
    "evidence",
    "follow_up_issue",
    "bad_case_evidence",
    "good_case_evidence",
)

REQUIRED_FINDING_FIELDS = (
    "id",
    "root_cause",
    "severity",
    "disposition",
    "scope",
    "summary",
    "evidence",
)

MANDATORY_BLOCK_SCOPES = frozenset({
    "central_claim",
    "acceptance",
    "regression",
    "security",
    "privacy",
    "authorization",
    "billing",
    "data_integrity",
    "ci",
    "verification",
    "review_tip",
})

FAMILY_KEYWORDS = {
    "deepseek": frozenset({"deepseek"}),
    "openai": frozenset({"openai", "gpt", "chatgpt", "o1", "o3", "o4"}),
    "anthropic": frozenset({"anthropic", "claude"}),
    "google": frozenset({"google", "googleai", "gemini", "bard"}),
    "xai": frozenset({"xai", "x", "grok"}),
    "zhipu": frozenset({"zhipu", "glm"}),
    "alibaba": frozenset({"alibaba", "aliyun", "tongyi", "qwen"}),
    "moonshot": frozenset({"moonshot", "kimi"}),
    "minimax": frozenset({"minimax", "abab"}),
    "xiaomi": frozenset({"xiaomi", "mimo"}),
}

FAMILY_DESIGNATORS = frozenset({
    "mini", "pro", "max", "plus", "ultra", "nano", "alpha", "beta",
    "preview", "flash", "turbo", "lite", "vision", "chat", "instruct",
    "coder", "codex", "reasoning", "thinking", "high", "medium", "low",
    "large", "small", "opus", "sonnet", "haiku", "next", "exp",
})

_VERSION_RE = re.compile(r"^\.?\d[\d.]*$")
_V_PREFIX_RE = re.compile(r"^v?\d+(\.\d+)?$")
_RELEASE_RE = re.compile(r"^r\d+$")
_SIZE_RE = re.compile(r"^\d+(\.\d+)?[bm]$")
_ALNUM_VERSION_RE = re.compile(r"^[a-z]?\d+[a-z]?$")


class ReviewValidationError(ValueError):
    pass


def _family_tokens(value: str) -> List[str]:
    normalized = value.strip().casefold()
    if not normalized:
        raise ReviewValidationError("model family must be a non-empty string")
    return re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", normalized)


def _is_generic_token(token: str) -> bool:
    return (
        token in FAMILY_DESIGNATORS
        or _VERSION_RE.fullmatch(token) is not None
        or _V_PREFIX_RE.fullmatch(token) is not None
        or _RELEASE_RE.fullmatch(token) is not None
        or _SIZE_RE.fullmatch(token) is not None
        or _ALNUM_VERSION_RE.fullmatch(token) is not None
    )


def _is_generic_suffix(suffix: str) -> bool:
    return (
        suffix in FAMILY_DESIGNATORS
        or _VERSION_RE.fullmatch(suffix) is not None
        or _V_PREFIX_RE.fullmatch(suffix) is not None
        or _RELEASE_RE.fullmatch(suffix) is not None
        or _SIZE_RE.fullmatch(suffix) is not None
    )


def _family_of_token(token: str) -> Optional[str]:
    for family, keywords in FAMILY_KEYWORDS.items():
        for keyword in keywords:
            if token == keyword:
                return family
            if token.startswith(keyword) and _is_generic_suffix(token[len(keyword):]):
                return family
    return None


def _resolve_family(value: str) -> str:
    tokens = _family_tokens(value)
    families = {fam for token in tokens if (fam := _family_of_token(token)) is not None}
    if not families:
        raise ReviewValidationError(f"unknown model family: {value!r}")
    if len(families) > 1:
        raise ReviewValidationError(f"ambiguous model family: {value!r}")
    family = next(iter(families))
    for token in tokens:
        token_family = _family_of_token(token)
        if token_family is not None and token_family != family:
            raise ReviewValidationError(f"ambiguous model family: {value!r}")
        if token_family is None and not _is_generic_token(token):
            raise ReviewValidationError(f"unknown model family: {value!r}")
    return family


def normalize_family(value: str) -> str:
    return _resolve_family(value)


def _require_mapping(value, context: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ReviewValidationError(f"{context} is not a mapping/object")
    return value


def _require_list(value, context: str) -> list:
    if not isinstance(value, list):
        raise ReviewValidationError(f"{context} is not a list")
    return value


def _require_non_empty_string(value, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewValidationError(f"{context} must be a non-empty string")
    return value


def _require_string_list(value, context: str) -> Tuple[str, ...]:
    items = _require_list(value, context)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ReviewValidationError(f"{context} must be a list of non-empty strings")
    return tuple(items)


def _optional_string_list(value, context: str) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    return _require_string_list(value, context)


def _reject_unknown_keys(mapping, allowed, context: str):
    unknown = [key for key in mapping if key not in allowed]
    if unknown:
        raise ReviewValidationError(
            f"unknown {context} key(s): {', '.join(sorted(unknown))}"
        )


def _enum_member(mapping, key, enum, context: str):
    raw = mapping[key]
    if not isinstance(raw, str) or raw not in enum._value2member_map_:
        raise ReviewValidationError(f"unsupported {context}: {raw!r}")
    return enum(raw)


def _parse_finding(raw_finding) -> Finding:
    finding = _require_mapping(raw_finding, "finding")
    _reject_unknown_keys(finding, FINDING_FIELDS, "finding")
    missing = [field for field in REQUIRED_FINDING_FIELDS if field not in finding]
    if missing:
        raise ReviewValidationError(f"missing finding field(s): {', '.join(missing)}")

    finding_id = _require_non_empty_string(finding["id"], "finding id")
    root_cause = _require_non_empty_string(finding["root_cause"], "finding.root_cause")
    summary = _require_non_empty_string(finding["summary"], "finding.summary")

    severity = _enum_member(finding, "severity", Severity, "severity")
    disposition = _enum_member(finding, "disposition", Disposition, "disposition")

    scope_raw = finding["scope"]
    if not isinstance(scope_raw, str) or not scope_raw.strip():
        raise ReviewValidationError("finding.scope must be a non-empty string")
    scope = scope_raw.strip().casefold()

    follow_up = finding.get("follow_up_issue")
    if disposition is Disposition.FOLLOW_UP_ISSUE:
        if follow_up is None:
            raise ReviewValidationError("FOLLOW_UP_ISSUE finding requires a follow_up_issue number")
        if not isinstance(follow_up, int) or isinstance(follow_up, bool) or follow_up <= 0:
            raise ReviewValidationError("follow_up_issue must be a positive integer")
    elif follow_up is not None:
        raise ReviewValidationError("follow_up_issue only applies to FOLLOW_UP_ISSUE findings")

    evidence = _require_string_list(finding["evidence"], "finding.evidence")

    bad_case = _optional_string_list(
        finding.get("bad_case_evidence"), "finding.bad_case_evidence"
    )
    good_case = _optional_string_list(
        finding.get("good_case_evidence"), "finding.good_case_evidence"
    )
    if (bad_case is None) != (good_case is None):
        raise ReviewValidationError(
            "bad_case_evidence and good_case_evidence must be supplied together"
        )

    return Finding(
        id=finding_id,
        root_cause=root_cause,
        severity=severity,
        disposition=disposition,
        scope=scope,
        summary=summary,
        evidence=evidence,
        follow_up_issue=follow_up,
        bad_case_evidence=bad_case or (),
        good_case_evidence=good_case or (),
    )


def _parse_family(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ReviewValidationError(f"{field} must be a non-empty string")
    return _resolve_family(raw)


def validate_review(record: Mapping) -> ReviewRecord:
    record = _require_mapping(record, "review record")
    _reject_unknown_keys(record, TOP_LEVEL_FIELDS, "review record")
    missing = [field for field in REQUIRED_TOP_LEVEL_FIELDS if field not in record]
    if missing:
        raise ReviewValidationError(f"missing field(s): {', '.join(missing)}")

    schema_version = record["schema_version"]
    if schema_version != 1:
        raise ReviewValidationError(f"unsupported schema_version: {schema_version!r}")

    repository = record["repository"]
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        raise ReviewValidationError("repository must be a 'owner/name' string")

    pr_number = record["pr_number"]
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ReviewValidationError("pr_number must be a positive integer")

    reviewed_branch = _require_non_empty_string(
        record["reviewed_branch"], "reviewed_branch"
    )

    reviewed_commit = record["reviewed_commit"]
    if not isinstance(reviewed_commit, str) or COMMIT_ID_RE.fullmatch(reviewed_commit) is None:
        raise ReviewValidationError(
            "reviewed_commit must be a 40-character lowercase hex commit ID"
        )

    has_base = "base_commit" in record
    has_range = "comparison_range" in record
    if has_base == has_range:
        raise ReviewValidationError("exactly one of base_commit or comparison_range is required")
    base_commit = None
    comparison_range = None
    if has_base:
        base_commit = record["base_commit"]
        if not isinstance(base_commit, str) or COMMIT_ID_RE.fullmatch(base_commit) is None:
            raise ReviewValidationError(
                "base_commit must be a 40-character lowercase hex commit ID"
            )
    else:
        comparison_range = _require_non_empty_string(
            record["comparison_range"], "comparison_range"
        )

    implementer_family = _parse_family(record["implementer_family"], "implementer_family")
    reviewer_family = _parse_family(record["reviewer_family"], "reviewer_family")
    if implementer_family == reviewer_family:
        raise ReviewValidationError(
            "implementer and reviewer must come from a different model family"
        )

    verdict = _enum_member(record, "verdict", Verdict, "verdict")

    local_evidence = _require_string_list(record["local_evidence"], "local_evidence")
    if not local_evidence:
        raise ReviewValidationError("local_evidence must be a non-empty list")
    ci_evidence = _require_string_list(record["ci_evidence"], "ci_evidence")
    if not ci_evidence:
        raise ReviewValidationError("ci_evidence must be a non-empty list")

    raw_findings = _require_list(record["findings"], "findings")
    findings = [_parse_finding(item) for item in raw_findings]

    seen_ids: set[str] = set()
    for finding in findings:
        if finding.id in seen_ids:
            raise ReviewValidationError(f"duplicate finding id: {finding.id}")
        seen_ids.add(finding.id)

    intentionally_not_findings = _require_string_list(
        record["intentionally_not_findings"], "intentionally_not_findings"
    )

    findings, verdict = _apply_mandatory_blocks_and_verdict(findings, verdict)
    return ReviewRecord(
        schema_version=schema_version,
        repository=repository,
        pr_number=pr_number,
        reviewed_branch=reviewed_branch,
        reviewed_commit=reviewed_commit,
        implementer_family=implementer_family,
        reviewer_family=reviewer_family,
        local_evidence=local_evidence,
        ci_evidence=ci_evidence,
        verdict=verdict,
        findings=findings,
        intentionally_not_findings=intentionally_not_findings,
        base_commit=base_commit,
        comparison_range=comparison_range,
    )


def _apply_mandatory_blocks_and_verdict(
    findings: List[Finding], declared_verdict: Verdict
) -> Tuple[List[Finding], Verdict]:
    promoted = []
    for finding in findings:
        if finding.disposition is not Disposition.BLOCKS_PR and finding.scope in MANDATORY_BLOCK_SCOPES:
            promoted.append(
                Finding(
                    id=finding.id,
                    root_cause=finding.root_cause,
                    severity=finding.severity,
                    disposition=Disposition.BLOCKS_PR,
                    scope=finding.scope,
                    summary=finding.summary,
                    evidence=tuple(finding.evidence),
                    bad_case_evidence=tuple(finding.bad_case_evidence),
                    good_case_evidence=tuple(finding.good_case_evidence),
                )
            )
        else:
            promoted.append(finding)

    has_blocker = any(f.disposition is Disposition.BLOCKS_PR for f in promoted)
    has_follow_up = any(f.disposition is Disposition.FOLLOW_UP_ISSUE for f in promoted)

    if has_blocker:
        verdict = Verdict.CHANGES_REQUIRED
    elif declared_verdict is Verdict.APPROVED_WITH_FOLLOW_UPS:
        if not has_follow_up:
            raise ReviewValidationError(
                "APPROVED_WITH_FOLLOW_UPS requires at least one valid follow-up finding"
            )
        verdict = Verdict.APPROVED_WITH_FOLLOW_UPS
    elif declared_verdict is Verdict.APPROVED:
        if has_follow_up:
            verdict = Verdict.APPROVED_WITH_FOLLOW_UPS
        else:
            verdict = Verdict.APPROVED
    else:
        verdict = declared_verdict

    return promoted, verdict