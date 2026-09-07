# Immutable Project Policy Floor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace rollbackable project-adoption authority with an immutable registry floor that permanently enforces the PublyApp review policy while preserving generic unregistered-repository behavior.

**Architecture:** Add a frozen repository-policy registry beside the existing model and launcher registries. Normalize every config through that registry; registered repositories must provide the released policy identity and exact route set, while adoption events are audit/migration evidence only. Keep event/head-anchor integrity and retirement recovery, but remove policy-authority decisions that depend on adoption existence.

**Tech Stack:** Python standard library, JSON Schema 2020-12, `unittest`, existing `tools/pr_closure` CLI/store.

---

### Task 1: Define and test the immutable repository floor

**Files:**
- Modify: `tools/pr_closure/registries.py`
- Modify: `tools/tests/test_registries.py`
- Modify: `tools/tests/test_policy_config.py`

- [ ] Add RED tests for exact lookup of `PublyApp/publyapp`, its policy ID/digest, exact Luna→Sol and current implementation routes, Anthropic prohibition, and immutable mapping behavior.
- [ ] Add RED tests that a missing, disabled, weaker, or altered registered-repository policy is rejected, while an exact staged/enforced mode-only variant is accepted.
- [ ] Add RED tests that `owner/repo` (or another unknown repository) has no floor and retains disabled generic behavior.
- [ ] Implement a frozen `RepositoryPolicyFloor` representation and versioned `REPOSITORY_POLICY_REGISTRIES` with a golden digest. Expose `policy_floor_for(repository)` returning `None` for unknown repositories and a verified floor for the exact key.
- [ ] Make the floor’s canonical policy identity include owner authorization, forbidden families, exceptions, and route identities; make comparison reject route removal, reviewer weakening, forbidden-family removal, and changed canonical identities.
- [ ] Run `PYTHONPATH=tools python3 -m unittest tools.tests.test_registries tools.tests.test_policy_config -v`; keep the expected RED output before implementation and GREEN output afterward.

### Task 2: Make configuration normalization enforce the floor

**Files:**
- Modify: `tools/pr_closure/model.py`
- Modify: `tools/pr_closure/contract.py`
- Modify: `tools/pr_closure/policy_lifecycle.py`
- Modify: `tools/tests/test_policy_config.py`
- Modify: `tools/tests/test_policy_reconciliation.py`

- [ ] Add RED tests proving exact PublyApp configurations validate without adoption state and weakened copies fail during `validate_project_config`.
- [ ] Add a normalized effective-policy helper that validates the local policy/routes first, looks up the repository floor, and returns the local configuration only when it satisfies the floor; preserve `ReviewPolicy()`/empty routes for unregistered repositories.
- [ ] Bind the registry floor digest to `policy_identity` and replace the old hard-coded rollback-only authority check with floor comparison. Keep activation/adoption functions available for audit/migration validation, but prevent them from weakening the effective config.
- [ ] Ensure an enforced registered config does not require `current_policy_adoption()` merely to load or validate.
- [ ] Run focused config/reconciliation tests and regenerate `tools/schemas/project-closure-v1.json` from the contract renderer; verify the generated schema remains structurally aligned.

### Task 3: Apply the same floor to review, status, and import

**Files:**
- Modify: `tools/pr_closure/review.py`
- Modify: `tools/pr_closure/cli.py`
- Modify: `tools/pr_closure/state.py`
- Modify: `tools/pr_closure/store.py`
- Modify: `tools/tests/test_review_policy.py`
- Modify: `tools/tests/test_cli.py`
- Modify: `tools/tests/test_store.py`

- [ ] Add RED tests for a fresh PublyApp store with no adoption, removed/disabled/weakened local policy, and legacy Claude v1 review: config/authority must fail before acceptance; no review file may be written by import.
- [ ] Add RED tests for an exact v2 Luna→Sol review with honest same-family provenance passing without a local adoption event.
- [ ] Add RED tests for rollback of valid pre-adoption `events.jsonl` and head prefixes with inodes preserved, and replacement of saved pre-adoption PR plus external-anchor directories: public status/import must remain fail-closed under the registered floor.
- [ ] Add RED tests for unknown repositories retaining documented generic behavior.
- [ ] Thread the normalized effective policy/routes into all review validators and status derivation. Remove registered-repository branches that make missing adoption disable policy or make schema-v1 acceptable.
- [ ] Make import perform effective-floor validation before reading/writing the durable review; make status use the same effective policy and classify malformed or mismatched registered config as an input/authority refusal.
- [ ] Narrow store method/docstring names and comments so adoption is described as audit/migration evidence. Retain event append integrity, crash replay, retirement, and historical migration checks.
- [ ] Run focused review/CLI/store tests and verify no pre-write side effect on rejected imports.

### Task 4: Add the registry-floor mutation kill

**Files:**
- Modify: `tools/tests/test_policy_reconciliation.py`
- Create: `tools/tests/test_policy_floor_mutation.py` only if the existing reconciliation harness cannot contain the mutation

- [ ] Add a disposable-copy mutation test that bypasses `policy_floor_for`/effective-floor lookup while leaving all local event and config checks intact.
- [ ] Execute the rollback and fresh-policy rejection tests against the mutated copy and assert at least one required test fails; do not modify the working tree with the mutation.
- [ ] Record the mutation command, killed test names, and clean-copy result for the final report.

### Task 5: Update truthful policy documentation

**Files:**
- Modify: `PLAYBOOK.md`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-09-05-same-family-review-policy-design.md`
- Modify: `docs/superpowers/plans/2026-09-05-same-family-review-policy.md`
- Modify: `docs/superpowers/plans/2026-09-05-publyapp-review-policy-rollout.md`

- [ ] Replace claims that adoption/event anchors are the sole or monotonic policy latch with the versioned registry-floor authority model.
- [ ] State that owner-mandated Sol-only adversarial reviews remain high/xhigh, that Luna→Sol is a same-OpenAI-family owner-authorized exception, and that Claude has no PublyApp authority.
- [ ] Describe adoption as audit/migration evidence and retain event integrity/crash-recovery claims only where they remain true.
- [ ] Search all changed docs for contradictory “sole authority”, “monotonic adoption”, or “event anchor prevents rollback” language and fix it.

### Task 6: Full verification and final handoff

- [ ] Run focused config/registry/review/status/import/rollback tests.
- [ ] Run the historical policy, retirement, migration, schema, and provenance suites.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools python3 -m unittest discover -s tools/tests -p 'test*.py' -q` exactly once after focused GREEN.
- [ ] Run `PYTHONPATH=tools python3 -m pr_closure.contract` and verify schema render agreement for all schemas.
- [ ] Run `PYTHONPYCACHEPREFIX=/var/tmp/policy-floor-pyc PYTHONPATH=tools python3 -m compileall -q tools/pr_closure tools/tests`, `git diff --check`, and Git status/HEAD checks.
- [ ] Write `/home/radan/.hermes/orchestration/runs/publyapp-2026-09-06-captain/policy-registry-floor-fix-report.md` with exact final SHA, narrowed/deleted anchor claims, RED/GREEN counts, focused/full commands, and mutation evidence. Do not write GitHub, Obsidian, ledger, push, PR, merge, review verdict, or co-author metadata.
- [ ] Commit the implementation with no `Co-Authored-By` trailer and confirm the worktree is clean.
