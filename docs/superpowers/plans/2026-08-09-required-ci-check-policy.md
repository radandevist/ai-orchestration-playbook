# Required CI Check Policy Implementation Plan

> **Historical, non-normative model routing and issue admission:** The Luna/`xhigh` instructions below record the policy used when this plan was written (2026-08-09) and are **superseded and non-normative**. Current dispatches must follow the live [PLAYBOOK model policy](../../../PLAYBOOK.md): exactly one cross-family GPT-5.6 Sol review at `high` (the final adversarial judgment before merge), with no reviewer shotgun, fallback cascade, or Claude-family calls. Issue admission follows the live §2.6 ladder; every admitted deferred root defect has one verified root issue.

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans with the AI orchestration playbook. DeepSeek V4 Flash implements at `max`; GPT-5.6 Luna at xhigh independently reviews. Do not push or merge without the owner approval recorded for this run.

**Goal:** Add an opt-in, fail-closed authoritative CI-check list to the PR closure gate.

**Architecture:** `ProjectConfig` carries an optional immutable tuple of exact GitHub check names.
The CI classifier selects and validates that set before applying existing outcome rules; an absent
or empty list retains today’s all-rollup behavior. PublyApp opts in with its aggregate gates.

**Tech Stack:** Python standard library, JSON Schema, `unittest`, GitHub CLI live status probe.

---

### Task 1: Contract and classifier tests

**Files:**
- Modify: `tools/tests/test_sources.py`
- Modify: `tools/tests/test_schema_agreement.py`
- Modify: `tools/tests/test_cli.py`

- [ ] Add failing classifier cases for an exact selected green set with unrelated skipped checks,
  an absent required name, duplicate live results for one required name, and a selected failed
  check. Assert only the selected green set is `PASSING`; absence/duplicates are never passing.
- [ ] Add failing configuration cases for blank and duplicate `ci_required_checks`, plus an empty
  list retaining all-rollup behavior.
- [ ] Run only the named new cases and confirm they fail before implementation.

### Task 2: Minimal policy implementation

**Files:**
- Modify: `tools/pr_closure/model.py`
- Modify: `tools/pr_closure/contract.py`
- Modify: `tools/pr_closure/sources.py`
- Modify: `tools/pr_closure/cli.py`
- Modify: `tools/schemas/project-closure-v1.json`

- [ ] Add optional tuple-valued `ci_required_checks` to `ProjectConfig` and validate non-blank,
  unique names in the contract/schema.
- [ ] Change `classify_ci(checks, head_oid, infra_event=None, required_checks=())` to select exact
  declared names before classification. Missing or duplicate names must yield non-passing evidence.
- [ ] Pass configuration policy only from `_build_snapshot`; do not change review, store, projection,
  or lease behavior.
- [ ] Run the Task 1 cases until green.

### Task 3: Regression proof and verification

**Files:**
- Modify: `tools/tests/test_sources.py`
- Modify: `tools/tests/test_cli.py`

- [ ] Add a safe mutation proof: temporarily bypass required-name selection and show the selected
  skipped-check regression fails, then restore exact source bytes.
- [ ] Run affected suites, full framework discovery, schema agreement, CLI help, and diff check.
- [ ] Update the durable implementation report with RED/mutation/GREEN evidence; commit only the
  intended source, schema, and test files.

### Task 4: PublyApp configuration adoption

**Files:**
- Modify: `/home/radan/.hermes/orchestration/projects/publyapp.json`

- [ ] Add PublyApp’s exact required names: `require-linked-issue`, `openapi-spec-drift-gate`,
  `docs-archive-gate`, `front-ci-gate`, `front-e2e-gate`, `old-front-unit`, and `old-front-e2e`.
- [ ] Re-run live statuses for all six PRs. A green required set must report `PASSING`; selected
  red gates must report `BRANCH_FAILURE`; a source failure must still be typed and non-mutating.
- [ ] Require an independent Luna review before advancing PR transitions.
