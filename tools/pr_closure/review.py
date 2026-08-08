from __future__ import annotations

import re
from typing import List, Mapping, Tuple

from pr_closure.model import Disposition, Finding, ReviewRecord, Severity, Verdict

COMMIT_ID_RE = re.compile(r"^[0-9a-f]{40}$")

TOP_LEVEL_FIELDS = (
    "schema_version",
    "repository",
    "pr_number",
    "reviewed_commit",
    "implementer_family",
    "reviewer_family",
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

FAMILY_PREFIXES = ("deepseek", "claude", "gpt")


class ReviewValidationError(ValueError):
    pass


def normalize_family(value: str) -> str:
    lowered = value.strip().lower()
    for prefix in FAMILY_PREFIXES:
        if lowered == prefix or lowered.startswith(prefix):
            return prefix
    return lowered


def _require_mapping(value, context: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ReviewValidationError(f"{context} is not a mapping/object")
    return value


def _require_list(value, context: str) -> list:
    if not isinstance(value, list):
        raise ReviewValidationError(f"{context} is not a list")
    return value


def _parse_finding(raw_finding):
    finding = _require_mapping(raw_finding, "finding")
    required = (f for f in FINDING_FIELDS if f != "follow_up_issue")
    missing = [k for k in required if k not in finding]
    if missing:
        raise ReviewValidationError(f"missing finding field(s): {', '.join(missing)}")

    severity_raw = finding["severity"]
    if severity_raw not in Severity._value2member_map_:
        raise ReviewValidationError(f"unsupported severity: {severity_raw}")
    disposition_raw = finding["disposition"]
    if disposition_raw not in Disposition._value2member_map_:
        raise ReviewValidationError(f"unsupported disposition: {disposition_raw}")

    evidence = _require_list(finding["evidence"], "finding.evidence")
    if not all(isinstance(item, str) for item in evidence):
        raise ReviewValidationError("finding.evidence must be a list of strings")

    severity = Severity(severity_raw)
    disposition = Disposition(disposition_raw)
    follow_up = finding.get("follow_up_issue")

    if disposition is Disposition.FOLLOW_UP_ISSUE:
        if follow_up is None:
            raise ReviewValidationError("FOLLOW_UP_ISSUE finding requires a follow_up_issue number")
        if not isinstance(follow_up, int) or isinstance(follow_up, bool) or follow_up <= 0:
            raise ReviewValidationError("follow_up_issue must be a positive integer")
    elif follow_up is not None:
        raise ReviewValidationError("follow_up_issue only applies to FOLLOW_UP_ISSUE findings")

    finding_id = finding["id"]
    if not isinstance(finding_id, str) or not finding_id.strip():
        raise ReviewValidationError("finding id must be a non-empty string")

    return Finding(
        id=finding_id,
        root_cause=finding["root_cause"],
        severity=severity,
        disposition=disposition,
        scope=finding["scope"],
        summary=finding["summary"],
        evidence=list(evidence),
        follow_up_issue=follow_up,
    )


def _parse_family(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ReviewValidationError(f"{field} must be a non-empty string")
    return raw


def validate_review(record: Mapping) -> ReviewRecord:
    record = _require_mapping(record, "review record")
    missing = [k for k in TOP_LEVEL_FIELDS if k not in record]
    if missing:
        raise ReviewValidationError(f"missing field(s): {', '.join(missing)}")

    schema_version = record["schema_version"]
    if schema_version != 1:
        raise ReviewValidationError(f"unsupported schema_version: {schema_version!r}")

    repository = record["repository"]
    if not isinstance(repository, str) or "/" not in repository:
        raise ReviewValidationError("repository must be a 'owner/name' string")

    pr_number = record["pr_number"]
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ReviewValidationError("pr_number must be a positive integer")

    reviewed_commit = record["reviewed_commit"]
    if not isinstance(reviewed_commit, str) or not COMMIT_ID_RE.match(reviewed_commit):
        raise ReviewValidationError("reviewed_commit must be a 40-character lowercase hex commit ID")

    implementer_family = _parse_family(record["implementer_family"], "implementer_family")
    reviewer_family = _parse_family(record["reviewer_family"], "reviewer_family")
    if normalize_family(implementer_family) == normalize_family(reviewer_family):
        raise ReviewValidationError(
            "implementer and reviewer must come from a different model family"
        )

    verdict_raw = record["verdict"]
    verdict = Verdict._value2member_map_.get(verdict_raw)
    if verdict is None:
        raise ReviewValidationError(f"unsupported verdict: {verdict_raw}")

    raw_findings = _require_list(record["findings"], "findings")
    findings = [_parse_finding(item) for item in raw_findings]

    seen_ids: set[str] = set()
    for finding in findings:
        if finding.id in seen_ids:
            raise ReviewValidationError(f"duplicate finding id: {finding.id}")
        seen_ids.add(finding.id)

    intentionally_not_findings = _require_list(
        record["intentionally_not_findings"], "intentionally_not_findings"
    )
    if not all(isinstance(item, str) for item in intentionally_not_findings):
        raise ReviewValidationError("intentionally_not_findings must be a list of strings")

    findings, verdict = _apply_mandatory_blocks_and_verdict(findings, verdict)
    return ReviewRecord(
        schema_version=schema_version,
        repository=repository,
        pr_number=pr_number,
        reviewed_commit=reviewed_commit,
        implementer_family=implementer_family,
        reviewer_family=reviewer_family,
        verdict=verdict,
        findings=findings,
        intentionally_not_findings=list(intentionally_not_findings),
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
                    evidence=finding.evidence,
                    follow_up_issue=None,
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