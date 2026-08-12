from __future__ import annotations

from typing import List, Mapping, Optional, Tuple

from pr_closure import contract
from pr_closure import families
from pr_closure.contract import (
    ALLOWED_FINDING_KEYS,
    ALLOWED_TOP_LEVEL_KEYS,
    EXACTLY_ONE_OF,
    PAIRED_PROOF,
    REQUIRED_FINDING_KEYS,
    REQUIRED_TOP_LEVEL_KEYS,
    ReviewValidationError,
)
from pr_closure.model import Disposition, Finding, ReviewRecord, Severity, Verdict

TOP_LEVEL_FIELDS = ALLOWED_TOP_LEVEL_KEYS
FINDING_FIELDS = ALLOWED_FINDING_KEYS

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


def normalize_family(value: str) -> str:
    return families.resolve_family(value)


def _parse_finding(raw_finding) -> Finding:
    finding = contract.require_mapping(raw_finding, "finding")
    contract.reject_unknown_keys(finding, ALLOWED_FINDING_KEYS, "finding")
    contract.require_present(finding, REQUIRED_FINDING_KEYS, "finding")
    values = contract.check(contract.FINDING_FIELDS, finding)
    severity = Severity(values["severity"])
    disposition = Disposition(values["disposition"])
    follow_up_issue = contract.check_follow_up(finding, disposition)
    if (PAIRED_PROOF[0] in finding) != (PAIRED_PROOF[1] in finding):
        raise ReviewValidationError(
            "bad_case_evidence and good_case_evidence must be supplied together"
        )
    return Finding(
        id=values["id"],
        root_cause=values["root_cause"],
        severity=severity,
        disposition=disposition,
        scope=values["scope"].strip().casefold(),
        summary=values["summary"],
        evidence=values["evidence"],
        follow_up_issue=follow_up_issue,
        bad_case_evidence=values.get("bad_case_evidence") or (),
        good_case_evidence=values.get("good_case_evidence") or (),
    )


def validate_review(record: Mapping) -> ReviewRecord:
    record = contract.require_mapping(record, "review record")
    contract.reject_unknown_keys(record, ALLOWED_TOP_LEVEL_KEYS, "review record")
    contract.require_present(record, REQUIRED_TOP_LEVEL_KEYS, "review record")
    values = contract.check(contract.RECORD_FIELDS, record, parsers={"findings": _parse_finding})

    has_base = EXACTLY_ONE_OF[0] in record
    has_range = EXACTLY_ONE_OF[1] in record
    if has_base == has_range:
        raise ReviewValidationError("exactly one of base_commit or comparison_range is required")

    implementer_family = families.resolve_family(record["implementer_family"])
    reviewer_family = families.resolve_family(record["reviewer_family"])
    if implementer_family == reviewer_family:
        raise ReviewValidationError(
            "implementer and reviewer must come from a different model family"
        )

    verdict = Verdict(values["verdict"])
    findings = list(values["findings"])
    seen_ids = set()
    for finding in findings:
        if finding.id in seen_ids:
            raise ReviewValidationError(f"duplicate finding id: {finding.id}")
        seen_ids.add(finding.id)
    findings, verdict = _apply_mandatory_blocks_and_verdict(findings, verdict)

    return ReviewRecord(
        schema_version=values["schema_version"],
        repository=values["repository"],
        pr_number=values["pr_number"],
        reviewed_branch=values["reviewed_branch"],
        reviewed_commit=values["reviewed_commit"],
        implementer_family=implementer_family,
        reviewer_family=reviewer_family,
        local_evidence=values["local_evidence"],
        ci_evidence=values["ci_evidence"],
        verdict=verdict,
        findings=findings,
        intentionally_not_findings=values["intentionally_not_findings"],
        base_commit=values.get("base_commit"),
        comparison_range=values.get("comparison_range"),
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


def require_live_binding(record, repository, pr_number, head_branch, head_commit) -> None:
    """Bind a validated review to the live PR facts (T6L-F3).

    One shared authority contract used by ``import-review`` writes and every
    status read: the durable review must match the configured repository, the
    requested pull request number, the live head branch, and the live head
    commit. Foreign repository/PR/branch/commit artifacts fail closed even
    when schema-valid and digest-bound.
    """
    if record.repository != repository:
        raise ReviewValidationError(
            "review repository does not bind to the configured repository"
        )
    if record.pr_number != pr_number:
        raise ReviewValidationError(
            "review pr_number does not bind to the requested pull request"
        )
    if record.reviewed_branch != head_branch:
        raise ReviewValidationError(
            "review reviewed_branch does not bind to the pull request head branch"
        )
    if record.reviewed_commit != head_commit:
        raise ReviewValidationError(
            "review reviewed_commit does not bind to the pull request head commit"
        )
