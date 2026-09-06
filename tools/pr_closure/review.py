from __future__ import annotations

import re
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
from pr_closure.model import (
    Disposition,
    Finding,
    ModelRoute,
    ProvenanceParticipant,
    ReviewPolicy,
    ReviewProvenance,
    ReviewRecord,
    Severity,
    Verdict,
)
from pr_closure.registries import RegistryValidationError, require_launcher, require_model

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


V2_EXTRA_KEYS = frozenset({
    "implementer_model",
    "reviewer_model",
    "review_exception",
    "provenance",
})
V2_ALLOWED_KEYS = frozenset(ALLOWED_TOP_LEVEL_KEYS) | V2_EXTRA_KEYS
_PROVENANCE_KEYS = frozenset({
    "registry_version",
    "launcher_registry_version",
    "implementer",
    "reviewer",
})
_PARTICIPANT_KEYS = frozenset({
    "model_id",
    "runner",
    "invocation_model",
    "run_ref",
    "durable_path",
    "sha256",
})
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _nonblank(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewValidationError("{0} must be a non-empty string".format(name))
    return value


def _parse_participant(raw, name: str) -> ProvenanceParticipant:
    item = contract.require_mapping(raw, name)
    contract.reject_unknown_keys(item, _PARTICIPANT_KEYS, name)
    contract.require_present(item, _PARTICIPANT_KEYS, name)
    digest = _nonblank(item["sha256"], name + ".sha256")
    if _DIGEST_RE.fullmatch(digest) is None:
        raise ReviewValidationError(name + ".sha256 must be lowercase 64-hex")
    durable_path = _nonblank(item["durable_path"], name + ".durable_path")
    if not durable_path.startswith("/"):
        raise ReviewValidationError(name + ".durable_path must be absolute")
    return ProvenanceParticipant(
        model_id=_nonblank(item["model_id"], name + ".model_id"),
        runner=_nonblank(item["runner"], name + ".runner"),
        invocation_model=_nonblank(
            item["invocation_model"], name + ".invocation_model"
        ),
        run_ref=_nonblank(item["run_ref"], name + ".run_ref"),
        durable_path=durable_path,
        sha256=digest,
    )


def _parse_provenance(raw) -> ReviewProvenance:
    item = contract.require_mapping(raw, "provenance")
    contract.reject_unknown_keys(item, _PROVENANCE_KEYS, "provenance")
    contract.require_present(item, _PROVENANCE_KEYS, "provenance")
    return ReviewProvenance(
        registry_version=_nonblank(item["registry_version"], "registry_version"),
        launcher_registry_version=_nonblank(
            item["launcher_registry_version"], "launcher_registry_version"
        ),
        implementer=_parse_participant(item["implementer"], "provenance.implementer"),
        reviewer=_parse_participant(item["reviewer"], "provenance.reviewer"),
    )


def _parse_review_exception(raw) -> str:
    item = contract.require_mapping(raw, "review_exception")
    contract.reject_unknown_keys(item, ("policy_id",), "review_exception")
    contract.require_present(item, ("policy_id",), "review_exception")
    return _nonblank(item["policy_id"], "review_exception.policy_id")


def _parse_common_review(record: Mapping, schema_version: int) -> ReviewRecord:
    legacy_shape = {key: value for key, value in record.items() if key in ALLOWED_TOP_LEVEL_KEYS}
    legacy_shape["schema_version"] = 1
    contract.require_present(legacy_shape, REQUIRED_TOP_LEVEL_KEYS, "review record")
    values = contract.check(
        contract.RECORD_FIELDS,
        legacy_shape,
        parsers={"findings": _parse_finding},
    )

    has_base = EXACTLY_ONE_OF[0] in legacy_shape
    has_range = EXACTLY_ONE_OF[1] in legacy_shape
    if has_base == has_range:
        raise ReviewValidationError("exactly one of base_commit or comparison_range is required")

    implementer_family = families.resolve_family(legacy_shape["implementer_family"])
    reviewer_family = families.resolve_family(legacy_shape["reviewer_family"])
    verdict = Verdict(values["verdict"])
    findings = list(values["findings"])
    seen_ids = set()
    for finding in findings:
        if finding.id in seen_ids:
            raise ReviewValidationError(f"duplicate finding id: {finding.id}")
        seen_ids.add(finding.id)
    findings, verdict = _apply_mandatory_blocks_and_verdict(findings, verdict)

    return ReviewRecord(
        schema_version=schema_version,
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


def _validate_v2_identity(
    parsed: ReviewRecord,
    raw: Mapping,
    review_policy: ReviewPolicy,
    model_routes: Tuple[ModelRoute, ...],
) -> ReviewRecord:
    implementer_model = _nonblank(raw["implementer_model"], "implementer_model")
    reviewer_model = _nonblank(raw["reviewer_model"], "reviewer_model")
    provenance = _parse_provenance(raw["provenance"])
    try:
        implementer_family = require_model(provenance.registry_version, implementer_model)
        reviewer_family = require_model(provenance.registry_version, reviewer_model)
        for model_id, participant in (
            (implementer_model, provenance.implementer),
            (reviewer_model, provenance.reviewer),
        ):
            if participant.model_id != model_id:
                raise ReviewValidationError(
                    "provenance participant model does not match review model"
                )
            require_launcher(
                provenance.registry_version,
                provenance.launcher_registry_version,
                participant.model_id,
                participant.runner,
                participant.invocation_model,
            )
    except RegistryValidationError as error:
        raise ReviewValidationError(str(error)) from error
    if (
        parsed.implementer_family != implementer_family
        or parsed.reviewer_family != reviewer_family
    ):
        raise ReviewValidationError("declared model family disagrees with canonical model registry")

    exception_id = None
    if "review_exception" in raw:
        exception_id = _parse_review_exception(raw["review_exception"])

    if review_policy.mode is None:
        if len(model_routes) > 0:
            raise ReviewValidationError("disabled policy requires an empty route table")
        if exception_id is not None:
            raise ReviewValidationError("disabled policy cannot authorize a review exception")
        if implementer_family == reviewer_family:
            raise ReviewValidationError(
                "implementer and reviewer must come from a different model family"
            )
    else:
        matches = [
            route for route in model_routes if route.implementer_model == implementer_model
        ]
        if len(matches) != 1:
            raise ReviewValidationError("implementer model does not match exactly one policy route")
        route = matches[0]
        if reviewer_family in review_policy.forbidden_reviewer_families:
            raise ReviewValidationError("reviewer family is forbidden by project policy")
        route_identity = (
            route.registry_version,
            route.launcher_registry_version,
            route.reviewer_model,
            route.reviewer_runner,
            route.reviewer_invocation_model,
            route.implementer_runner,
            route.implementer_invocation_model,
        )
        record_identity = (
            provenance.registry_version,
            provenance.launcher_registry_version,
            reviewer_model,
            provenance.reviewer.runner,
            provenance.reviewer.invocation_model,
            provenance.implementer.runner,
            provenance.implementer.invocation_model,
        )
        if route_identity != record_identity:
            raise ReviewValidationError("review provenance does not match the exact policy route")
        if implementer_family == reviewer_family:
            if exception_id is None or exception_id != route.same_family_policy_id:
                raise ReviewValidationError("same-family review lacks its exact policy exception")
            exceptions = {
                item.id: item for item in review_policy.same_family_exceptions
            }
            exception = exceptions.get(exception_id)
            if (
                exception is None
                or exception.registry_version != provenance.registry_version
                or exception.implementer_family != implementer_family
                or exception.reviewer_model != reviewer_model
            ):
                raise ReviewValidationError("review exception does not match project policy")
        elif exception_id is not None or route.same_family_policy_id is not None:
            raise ReviewValidationError("cross-family review must not claim an exception")

    return ReviewRecord(
        **{
            name: getattr(parsed, name)
            for name in (
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
        },
        implementer_model=implementer_model,
        reviewer_model=reviewer_model,
        review_exception_id=exception_id,
        provenance=provenance,
    )


def validate_review(
    record: Mapping,
    *,
    review_policy: Optional[ReviewPolicy] = None,
    model_routes: Tuple[ModelRoute, ...] = (),
) -> ReviewRecord:
    record = contract.require_mapping(record, "review record")
    contract.reject_unknown_keys(record, V2_ALLOWED_KEYS, "review record")
    schema_version = record.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ReviewValidationError(
            "unsupported schema_version: {0!r}".format(schema_version)
        )
    policy = review_policy or ReviewPolicy()
    if schema_version == 1:
        if policy.mode is not None:
            raise ReviewValidationError(
                "schema v1 review cannot be authoritative under an active review policy"
            )
        if len(model_routes) > 0:
            raise ReviewValidationError("disabled policy requires an empty route table")
        parsed = _parse_common_review(record, 1)
        contract.reject_unknown_keys(record, ALLOWED_TOP_LEVEL_KEYS, "review record")
        if parsed.implementer_family == parsed.reviewer_family:
            raise ReviewValidationError(
                "implementer and reviewer must come from a different model family"
            )
        return parsed
    if schema_version == 2:
        contract.reject_unknown_keys(record, V2_ALLOWED_KEYS, "review record")
        contract.require_present(
            record,
            tuple(REQUIRED_TOP_LEVEL_KEYS) + (
                "implementer_model",
                "reviewer_model",
                "provenance",
            ),
            "review record",
        )
        return _validate_v2_identity(
            _parse_common_review(record, 2),
            record,
            policy,
            tuple(model_routes),
        )
    raise ReviewValidationError("unsupported schema_version: {0!r}".format(schema_version))


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
