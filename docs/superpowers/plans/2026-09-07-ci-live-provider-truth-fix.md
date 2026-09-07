# CI-Live Provider-Truth Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five remaining CI-live provider-truth blockers with provider-faithful tests, strict identity/metadata validation, and a non-vacuous full-status Git reread regression.

**Architecture:** Keep the existing fail-closed `GitHubSource` and CLI status flow. Resolve a strictly validated single workflow filename through GitHub’s documented direct workflow endpoint; bind check runs to current workflow-run and attempt responses using the run ID parsed from a repo-qualified Actions job URL; enforce exact attempt head and artifact expiry metadata; extend the existing CLI fake to mutate Git facts only on the final status reread.

**Tech Stack:** Python 3, `unittest`, GitHub REST via `gh api`, existing `pr_closure` source/CLI modules.

---

### Task 1: Workflow endpoint contract

**Files:**
- Modify: `tools/pr_closure/sources.py` (`GitHubSource._workflow_id`)
- Test: `tools/tests/test_live_pr_closure_fixes.py`
- Test: `tools/tests/test_cli.py` fake GitHub provider shape

- [ ] Write tests for direct filename success, returned path mismatch, malformed numeric ID, and a `total_count=101` list fixture proving the implementation does not accept a page-one-only workflow list.
- [ ] Run the focused workflow tests and verify the new cases fail against the current list-only implementation.
- [ ] Validate the configured path grammar as exactly `.github/workflows/<single filename>`, URL-encode the filename path segment, call `repos/{repo}/actions/workflows/{filename}`, and require numeric `id` plus exact returned `.github/workflows/<filename>` path.
- [ ] Run the focused workflow tests and verify they pass while existing qualified-path and pagination behavior remains intact.

### Task 2: Provider-faithful check-suite/run/attempt binding

**Files:**
- Modify: `tools/pr_closure/sources.py` (`_workflow_run`, `_check_run_candidate`)
- Test: `tools/tests/test_live_pr_closure_fixes.py`
- Test: `tools/tests/test_cli.py` fake GitHub provider shape

- [ ] Replace invented `GET /check-suites/{id}.workflow_run` fixtures and production reads with documented check-suite objects plus current workflow-run reads; derive the run ID only from the strict repo-qualified `/actions/runs/<positive-int>/job/<positive-int>` details URL.
- [ ] Add provider-faithful success and malformed/multiple/zero check-suite-to-attempt mapping tests, including current and attempt endpoints and exact `check_suite.id` matching.
- [ ] Run the focused binding tests and verify the new tests fail against the invented nested shape.
- [ ] Implement cached current-run and attempt reads, map each candidate suite ID to exactly one attempt response for the current run attempt, and reject wrong repo/path/run/job, malformed current/attempt data, and zero/multiple suite matches.
- [ ] Run the focused binding tests and verify they pass.

### Task 3: Exact attempt head and artifact expiry

**Files:**
- Modify: `tools/pr_closure/sources.py` (`_check_run_candidate`, `_read_snapshot`)
- Test: `tools/tests/test_live_pr_closure_fixes.py`

- [ ] Add a failing attempt-head mutation test and a parameterized artifact expiry test for missing, true, false, null, string, and numeric values.
- [ ] Run those tests and verify they fail because attempt head and expiry are currently under-validated.
- [ ] Require `attempt_workflow.head_sha == head_oid` and require `type(expired) is bool and expired is False`.
- [ ] Run the focused tests and verify they pass without changing the non-blocking UTF-8 normalization behavior.

### Task 4: Full status-path Git reread proof

**Files:**
- Modify: `tools/tests/test_cli.py` fake Git provider and `StatusCommandTests`
- No production modification expected unless the new test exposes a wiring defect.

- [ ] Add a sequential fake-Git control that returns unchanged initial facts and changed commit/branch/cleanliness fields only after the CI/status work, then require `status` exit 3.
- [ ] Run the test against the current implementation and verify RED if the fake sequence is not consumed by the final reread.
- [ ] Make the smallest production wiring change only if required, then run the test and verify GREEN.
- [ ] In a disposable copy, bypass only the final Git read/assertion and rerun the focused test; require failure of the mutation test while retaining the existing PR and cache mutation kills.

### Task 5: Integrated verification and report

**Files:**
- Create: `/home/radan/.hermes/orchestration/runs/publyapp-2026-09-06-captain/ci-live-provider-truth-fix-report.md`

- [ ] Run focused source/CLI/live tests with witnessed RED/GREEN counts, then run full discovery exactly once.
- [ ] Run schema/render comparison, external-cache `compileall`, diff checks, and Git cleanliness checks.
- [ ] Independently rerun provider probes and all three disposable mutation kills, including the new Git full-path mutation.
- [ ] Review the final diff against the five findings and write the report with exact final SHA, endpoint/shape contracts, RED/GREEN counts, mutation results, and dispositions.
- [ ] Commit the implementation and tests with no `Co-Authored-By`, then verify the final commit, clean worktree, and report contents.
