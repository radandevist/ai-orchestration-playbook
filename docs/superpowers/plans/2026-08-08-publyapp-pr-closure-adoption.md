# PublyApp PR Closure Adoption Plan

> **Historical, non-normative model routing and issue admission:** The Luna/`xhigh` instructions below record the policy used when this plan was written (2026-08-08) and are **superseded and non-normative**. Current dispatches must follow the live [PLAYBOOK model policy](../../../PLAYBOOK.md): exactly one cross-family GPT-5.6 Sol review at `high` (the final adversarial judgment before merge), with no reviewer shotgun, fallback cascade, or Claude-family calls. Issue admission in this document is likewise superseded: follow the live §2.6 ladder (`BLOCKS_PR` → link existing root issue → `FOLLOW_UP_ISSUE` only for a concrete reproducible independent root defect → `NOTE_ONLY`); the default is not a new issue, and every admitted deferred root defect has one verified root issue.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans together with the AI orchestration playbook. DeepSeek V4 Flash implements at `max`; GPT-5.6 Luna at `xhigh` independently reviews every pushed tip. Do not merge.

**Goal:** Make PublyApp the first live user of the mechanical closure gate and drive PRs #1054, #1061, #1065, #1066, #1078, and #1083 to an evidence-backed `APPROVED`, `APPROVED_WITH_FOLLOW_UPS`, or genuine `NEEDS_OWNER` state.

**Architecture:** Runtime state lives outside the PublyApp repository under the durable Hermes orchestration directory, while the repo adapter points agents to the portable gate. Each existing PR keeps its own branch and resolved worktree. The orchestrator derives one next action per PR from live GitHub, local Git, verification, and structured cross-family review records; Trello mirrors that state but never decides it.

**Tech Stack:** `pr-closure` framework, Git, GitHub CLI/API, DeepSeek V4 Flash implementation lane, GPT-5.6 Luna `xhigh` review lane, Node.js 24, pnpm, Playwright/Docker Compose, Trello MCP projection.

**Dependency:** Complete `docs/superpowers/plans/2026-08-08-pr-closure-framework.md` first. Do not emulate the gate with shell conditionals if the framework is not ready.

---

## File map

- Create `/home/radan/.hermes/orchestration/projects/publyapp.json` — user-scoped live configuration; contains no secret.
- Create `/home/radan/.hermes/orchestration/closure/publyapp/` — append-only evidence and review records.
- Create `/home/radan/.hermes/orchestration/runs/publyapp-pr-closure-20260808/` — briefs, worker outputs, and monitor state.
- Modify `/home/radan/Projects/PublyApp/publyapp/.ai/orchestration-adapter.md` — point future agents to the closure configuration and mandatory transition check, on an isolated branch only.
- Modify `/home/radan/Projects/PublyApp/publyapp/.gitignore` only if a repo-local generated path is introduced; the preferred design introduces none.
- Update PublyApp Trello cards — projection only, explicitly targeting the PublyApp board.
- Update the six existing PR branches — only fixes required by their own CI/review evidence.

## Non-negotiable operating constraints

- Never modify `develop` directly and never merge a PR.
- Resolve every worktree with `git worktree list --porcelain`; never construct a path from a PR number.
- Preserve staged, dirty, and untracked user work. Stop a lane if its resolved worktree is not a clean named checkpoint.
- Before every worker dispatch, append the required preflight ledger row with `decision: dispatch`.
- DeepSeek V4 Flash is the default implementer, invoked through the configured project harness at `max`; use the explicit OpenModel provider wiring from the playbook packet template.
- GPT-5.6 Luna at `xhigh` is the default cross-family reviewer for these PRs; record the ledgered pre-merge escalation reason. No new Claude model calls. A reviewer never edits the branch.
- Serialize Docker/Playwright/full-suite work with the framework's heavy-job lease. Lightweight source fixes and focused unit tests may run concurrently only when they use different worktrees.
- Every approval is bound to the exact pushed 40-character commit reported by GitHub. A push invalidates the prior approval automatically.
- Temporary mutation probes must live outside the tracked tree or be removed and proven absent before review can approve.
- Trello API failure means projection state is unknown; it never means an empty board or successful sync.

### Task 1: Install the live PublyApp closure configuration

**Files:**
- Create: `/home/radan/.hermes/orchestration/projects/publyapp.json`
- Create: `/home/radan/.hermes/orchestration/closure/publyapp/`
- Test: framework CLI configuration checks

- [ ] **Step 1: Write the configuration to a temporary candidate path**

Use the framework schema and these values:

```json
{
  "schema_version": 1,
  "project": "publyapp",
  "repository": "radandevist/publyapp",
  "repo_path": "/home/radan/Projects/PublyApp/publyapp",
  "default_branch": "develop",
  "closure_state_dir": "/home/radan/.hermes/orchestration/closure",
  "local_review_ready_commands": [
    "eval \"$(fnm env)\" && fnm use 24 && pnpm --filter front typecheck",
    "eval \"$(fnm env)\" && fnm use 24 && pnpm --filter front test"
  ],
  "closure_acceptance_commands": [
    "eval \"$(fnm env)\" && fnm use 24 && just ci",
    "eval \"$(fnm env)\" && fnm use 24 && just ci-full"
  ],
  "infra_retry_budget": 1,
  "stagnation_budget_minutes": 240,
  "heavy_job_limit": 1,
  "tracking_projection": "trello:publyapp"
}
```

The executor may narrow review-ready commands to a PR's touched surface, but closure acceptance must use the adapter's full gate. API-contract changes additionally require `just build-api && just generate-client && pnpm --filter front typecheck`.

- [ ] **Step 2: Prove invalid configuration fails closed**

Run the CLI once with the repository field removed and once with a relative state path.

Expected: exit 2 with a precise schema/config error; no state directory or event is written.

- [ ] **Step 3: Validate and atomically install the real configuration**

Run:

```bash
PYTHONPATH=/home/radan/ai-orchestration-playbook/tools \
  /home/radan/ai-orchestration-playbook/tools/pr-closure status \
  --config /tmp/publyapp-closure-candidate.json --pr 1054 --json
```

After successful validation, install the candidate at the user-scoped path with mode `0600`. The file contains paths and board routing only, never tokens.

- [ ] **Step 4: Verify configuration and directory permissions**

Run `stat` on both paths and rerun `status` with the installed config. Expected: source data is either live and valid or a typed source failure; never an empty-success fallback.

### Task 2: Import the six PRs from live sources

**Files:**
- Create: `/home/radan/.hermes/orchestration/closure/publyapp/pr-*/events.jsonl`
- Create: `/home/radan/.hermes/orchestration/runs/publyapp-pr-closure-20260808/inventory.json`

- [ ] **Step 1: Fetch exact live PR metadata**

For each of `1054 1061 1065 1066 1078 1083`, record `headRefName`, `headRefOid`, draft/state, review decision, checks, base branch, and URL through `gh pr view --json`. Record the retrieval timestamp and command exit status.

- [ ] **Step 2: Resolve every branch's actual worktree**

Parse `git worktree list --porcelain`. Match by branch ref, then confirm `git rev-parse HEAD`, `git rev-parse origin/<branch>`, and `git status --porcelain=v1 --untracked-files=all`.

Stop that PR at `UNVERIFIED` if no worktree exists, more than one candidate matches, the tree is dirty, or local/remote/GitHub tips disagree. Creating or repairing a worktree is a separate allowed transition, not an implicit side effect of import.

- [ ] **Step 3: Record the initial evidence without trusting old summaries**

Run `pr-closure status --json` for all six PRs and append one import event per PR. The expected initial categories from the 2026-08-08 audit are hints only:

- PRs #1054 and #1061: CI green with unresolved local adversarial blockers.
- PR #1065: branch lint failure plus browser-test failures.
- PR #1066: likely infrastructure failure before tests started, plus unresolved review work.
- PR #1078: CI green with an in-progress adversarial review.
- PR #1083: linked-issue/body gate failure plus browser-test failures.

If live evidence differs, the live evidence wins and the discrepancy is recorded.

- [ ] **Step 4: Verify fail-closed import**

Temporarily use an invalid GitHub repository name for one read. Expected: the run records `SourceUnavailable`, does not create six empty/closed states, and performs no Trello write.

### Task 3: Convert existing review output into structured records

**Files:**
- Create: `/home/radan/.hermes/orchestration/closure/publyapp/pr-*/reviews/<commit>/*.json`
- Create: `/home/radan/.hermes/orchestration/runs/publyapp-pr-closure-20260808/briefs/*.md`

- [ ] **Step 1: Inventory only reviews that name an exact commit**

Read the latest durable local review artifacts for each PR. Ignore prose approval that does not identify the reviewed commit or distinguish whole-PR approval from milestone approval.

- [ ] **Step 2: Normalize each finding on two axes**

Every finding receives `severity`, `disposition`, `scope`, `root_cause`, evidence, and a stable ID. Promote central-claim, acceptance, regression, security, privacy, authorization, billing, data-integrity, CI, verification, and review-tip findings to `BLOCKS_PR` mechanically.

- [ ] **Step 3: Route eligible non-blockers immediately**

> **Superseded by the live issue-admission ladder (PLAYBOOK §2.6).** The severity-driven filing instruction below contradicts current policy and is non-normative: disposition is decided by the admission ladder (`BLOCKS_PR` → link existing root issue → `FOLLOW_UP_ISSUE` only for a concrete, reproducible, independent root defect → `NOTE_ONLY`), never by severity labels alone. Improvements, style preferences, theoretical hardening, and unproven uncertainty are `NOTE_ONLY`. The default is not a new issue: file only for a step-3 admission, reuse an open issue for the same root cause, and never mint successor issues for the same cause.

~~For `MEDIUM`, `MINOR`, or `NOTE` findings outside the changed central claim, verify that the problem still exists and is independently actionable. File a GitHub follow-up issue, record its issue number, and use `FOLLOW_UP_ISSUE`. Do not file duplicates; search open issues first.~~

- [ ] **Step 4: Generate one current fix packet per blocked PR**

The packet contains the exact pushed tip, blocking IDs, root-cause history, acceptance command, and negative proof required. It opens with:

```text
You are the IMPLEMENTER. Write every line yourself, directly. This is a focused coding task, NOT an orchestration task.
```

It forbids reading orchestration material, spawning agents, switching branches/worktrees, pushing, or merging. It requires the implementer to report any brief statement found wrong.

- [ ] **Step 5: Trigger design reset for repeated root causes**

If the same root cause has survived two correction rounds, enter `DESIGN_RESET`. Commission a fresh GPT-5.6 Luna reviewer to reproduce the mechanism, author a structural fix packet with a paired regression test, prove the test fails under the intended revert, revert any review probe, and return the packet. Do not send another orchestrator-written syntax patch.

### Task 4: Clear red CI before deeper review work

**Files:**
- Modify: only the relevant existing PR branches
- Test: failing CI command plus the PR's targeted tests

- [ ] **Step 1: Classify PR #1066's failed browser job**

Inspect the current Actions log. Mark `CI_INFRA_RETRY` only if the failed job identifies an infrastructure/cache/setup failure and no application test step began. Consume the single retry budget by rerunning only failed jobs.

Expected: a green retry returns the PR to its review-derived state. A second equivalent failure becomes `NEEDS_OWNER`; a code/test failure becomes `CI_RED`.

- [ ] **Step 2: Fix PR #1065's lint failure test-first**

In its resolved clean worktree, reproduce the nested-ternary oxlint failure at the live line. Dispatch one DeepSeek V4 Flash fix lane. Require the smallest behavior-preserving control-flow rewrite and a focused script test before the full front gate.

- [ ] **Step 3: Reproduce PR #1065's browser failures**

Acquire the heavy-job lease, start the exact adapter Compose stack, run the failing breadcrumb specs first, and save traces. Diagnose timeouts before changing application code. Dispatch a bounded DeepSeek fix only after the failing mechanism is demonstrated.

- [ ] **Step 4: Repair PR #1083's linkage metadata**

Confirm the PR body still lacks a closing keyword. Update it through `gh api -X PATCH` to use the correct `Closes #...` form because the adapter records `gh pr edit` as unreliable. This metadata-only action does not require an implementation lane, but the new body must be read back and recorded.

- [ ] **Step 5: Reproduce and fix PR #1083's browser failures**

Under the heavy-job lease, run only the failing marketing-shell and mobile-navigation specs first. Determine whether the selectors, fixtures, route behavior, or product code changed. Dispatch DeepSeek with the reproduced cause and require a negative control.

- [ ] **Step 6: Push only verified fixes and invalidate old reviews**

For each changed branch: run targeted tests, local review-ready commands, commit intentionally, push the existing branch, reread the GitHub head OID, and record the new tip. The gate must mark every review of the previous tip stale.

### Task 5: Close the green-but-blocked review loops

**Files:**
- Modify: PR branches #1054, #1061, and #1066 only when structured blockers require code changes
- Create: structured review and verification records for all affected PRs

- [ ] **Step 1: Put PR #1054 into design reset**

Its repeated central guard weakness must not receive another narrow allowlist/syntax patch. Have a fresh GPT-5.6 Luna reviewer reproduce the surviving bypass, map sibling paths, author the structural packet and paired mutation proof, then revert the probe. Dispatch DeepSeek against that packet.

- [ ] **Step 2: Apply PR #1061's latest exact blockers**

Normalize the drawer-contrast findings against the current pushed tip. Central contrast calculation, the real translucent surface, and caller override bypasses are blockers; unrelated design observations become verified issues. Dispatch one bounded DeepSeek lane for the blocker set.

- [ ] **Step 3: Apply PR #1066's review blockers after CI classification**

Do not mix infrastructure retry with code remediation. Once CI has a real result, normalize the latest review against the current tip and dispatch only confirmed blockers.

- [ ] **Step 4: Verify every implementation return before accepting it**

Reject exit 0 with empty or implausibly tiny output. Confirm the expected diff exists, the branch did not change, no agent/tool was spawned, focused tests passed, and temporary artifacts are absent. If output or worktree evidence contradicts the report, record `UNVERIFIED`.

- [ ] **Step 5: Push and record new exact tips**

Use each PR's existing branch. Never force-push unless a separately approved rebase requires it. After push, assert local HEAD = remote branch = GitHub head, then start review from that commit only.

### Task 6: Finish fresh adversarial reviews for all six pushed tips

**Files:**
- Create: one JSON review record and one concise Markdown companion per current pushed tip

- [ ] **Step 1: Cleanly finish PR #1078's current review**

Check whether the previously running reviewer completed. Before importing it, prove any temporary probe is absent and the worktree is clean. If the reviewer output is incomplete, stale, or tied to a different tip, discard it as authority and start a fresh review.

- [ ] **Step 2: Review each current tip with GPT-5.6 Luna at `xhigh`**

The review brief includes the originating issue/spec, full diff from merge base, repo standards, prior blocker IDs, exact pushed tip, and required commands. The reviewer is read-only and returns the versioned JSON record plus a concise human report.

- [ ] **Step 3: Demand causal tests, not green-only tests**

For new guards or regression tests, require a safe mutation/revert that makes the intended assertion fail. A test that fails only during setup, hangs, or agrees with the mutation does not prove the claim.

- [ ] **Step 4: Apply the multi-floor outcome mechanically**

- `CRITICAL`/`MAJOR` does not automatically block solely by label, but mandatory scope always blocks.
- Any broken central claim, acceptance criterion, regression, security/privacy/auth/billing/data-integrity invariant, CI gate, verification proof, or reviewed-tip match is `BLOCKS_PR`.
- Verified independent non-blockers must have a filed issue before `APPROVED_WITH_FOLLOW_UPS`.
- Notes that require no work use `NOTE_ONLY`.
- Missing evidence, malformed output, or disagreement between prose and JSON is `INCONCLUSIVE`.

- [ ] **Step 5: Repeat only from a changed root cause**

If review finds a new blocker, return to the relevant fix task. If it finds the same root cause again, enter `DESIGN_RESET`; do not count or reward another review round.

### Task 7: Run final acceptance and transition checks

**Files:**
- Create: `/home/radan/.hermes/orchestration/closure/publyapp/pr-*/verification/<commit>.json`

- [ ] **Step 1: Queue closure acceptance one PR at a time**

Acquire the heavy-job lease. Run `just ci`; run `just ci-full` when the PR touches browser behavior or its acceptance requires e2e. Always tear down Compose with volumes after the run, including on failure.

- [ ] **Step 2: Reconcile with GitHub checks**

Wait for the same pushed tip's required GitHub checks. Local green cannot replace red GitHub CI, and GitHub green cannot replace the adapter's stronger local backend gate.

- [ ] **Step 3: Check the exact terminal transition**

For each PR, run:

```bash
PYTHONPATH=/home/radan/ai-orchestration-playbook/tools \
  /home/radan/ai-orchestration-playbook/tools/pr-closure check-transition \
  --config /home/radan/.hermes/orchestration/projects/publyapp.json \
  --pr <number> --to <APPROVED-or-APPROVED_WITH_FOLLOW_UPS>
```

Expected: exit 0 only when local/remote/GitHub/review/verification commits are identical, CI and acceptance are green, no blocking finding remains, every follow-up has an issue, and the worktree is clean.

- [ ] **Step 4: Handle genuine owner decisions narrowly**

Use `NEEDS_OWNER` only for an actual product/policy choice, exhausted infrastructure retry, or permission/external-service blocker. The record must ask one concrete question. Age, complexity, or another review round is not an owner blocker.

### Task 8: Project closure state to Trello

**Files:**
- Update: existing PublyApp Trello cards or create one card per PR only when no matching card exists

- [ ] **Step 1: Resolve the PublyApp board explicitly**

Read the board identity before every sync. Do not rely on the DigitalPrevention board being the currently selected/default board.

- [ ] **Step 2: Dry-run the projection**

Map mechanical states to the board's existing lists/labels. The card description/comment includes current state, exact current commit abbreviation, next action, blocker IDs, and follow-up issue links. It does not include secrets or raw logs.

- [ ] **Step 3: Apply only from complete source data**

Run `sync --dry-run`, inspect the proposed changes, then `sync --apply`. If GitHub, Git, review, or Trello reads fail, apply nothing and report the typed failure.

- [ ] **Step 4: Prove idempotence**

Run the dry-run again. Expected: no changes. Trello disagreement never changes the closure state; the next successful sync repairs Trello from authority.

### Task 9: Land the durable PublyApp adapter binding

**Files:**
- Modify: `/home/radan/Projects/PublyApp/publyapp/.ai/orchestration-adapter.md`
- Test: adapter contract probe and Markdown checks

- [ ] **Step 1: Create an isolated adapter branch from current `origin/develop`**

Use a new provisional worktree under `.worktrees/`. Do not use the main `develop` checkout and do not add this change to any of the six feature branches.

- [ ] **Step 2: Add the mandatory closure binding**

Add fields for:

```text
closure_config = /home/radan/.hermes/orchestration/projects/publyapp.json
closure_gate = /home/radan/ai-orchestration-playbook/tools/pr-closure
review_record_schema = /home/radan/ai-orchestration-playbook/tools/schemas/review-record-v1.json
review_publication_cmd = gh api ...
tracking_projection = explicit PublyApp Trello board
```

State: orchestrated PR work stops before dispatch if the gate/config/schema is absent; no PR may be called approved without a successful exact-tip transition check.

- [ ] **Step 3: Add a machine-readable adapter contract test if the repo has an existing adapter/AI-doc check seam**

Search with CodeGraph first, then existing repo checks. Reuse the current documentation/AI-instruction test mechanism rather than inventing a parallel parser. If no seam exists, add a focused dependency-free test beside the nearest existing guard.

- [ ] **Step 4: Verify and open a small dedicated PR**

Run the focused contract test, `git diff --check`, and the relevant lightweight documentation gate. Commit, push, and open a draft PR only after the six original PRs have reached terminal closure states, so adoption does not distract from closing them. Do not merge.

### Task 10: Final audit and handoff

**Files:**
- Update: closure run summary and Obsidian session note

- [ ] **Step 1: Produce a six-row closure audit**

For each PR show: current pushed tip abbreviation, CI, local acceptance, review verdict, blocking IDs, follow-up issue IDs, mechanical state, and next owner action. Generate it from records; do not hand-maintain a second truth table.

- [ ] **Step 2: Verify no unauthorized integration occurred**

Confirm `develop` was not modified locally, no PR was merged, all changes are on intended branches, and user-owned untracked files remain intact.

- [ ] **Step 3: Verify the framework changed behavior, not only documentation**

Demonstrate at least these live outcomes: a stale approval rejected after push, a branch CI failure taking precedence over review, a non-blocker accepted only after issue filing, a repeated root cause entering design reset, and a Trello dry-run reflecting authoritative state.

- [ ] **Step 4: Update durable memory without transient task state**

Update the existing PublyApp session note with the durable lessons and artifact paths. Do not store raw logs, full transcripts, PR numbers, or commit hashes in Obsidian.

- [ ] **Step 5: Notify the owner only at terminal review readiness or a genuine blocker**

Report which PRs are ready for owner merge decisions and which single concrete decision, if any, remains. Include recovery instructions for any infrastructure-only failure. Do not claim completion unless the verification evidence is fresh.
