# AI Orchestration Playbook

> The portable operating model for an **orchestrating agent** running a batch of work through executors. Read this top-to-bottom before starting an orchestration run, then bind it to the current project via that repo's adapter (see §4).

## §0 — Preamble

**What this is.** A single, self-contained procedure for orchestrating multi-task software work: decompose a goal, dispatch parallel executors in isolated worktrees, review their output, integrate it, and close out the tracking issues — without the orchestrator ever executing project code itself.

**Who reads it.** Any orchestrating agent — Claude, Hermes, or a future agent. The document is agent-neutral. Agent-specific quirks belong in that agent's own global instructions, never here.

**Proactive usage rule.** An orchestrating agent should load and follow this playbook automatically when multi-step software work is detected, not wait for the human to ask. It is up to the orchestrator to judge applicability: read-only advisory, trivial one-step edits, or pure planning without execution do not need the full playbook. In ambiguous cases, ask rather than skip.

**How to use it.** Before starting an orchestration run in a repository, **read that repo's `.ai/orchestration-adapter.md`** (§4). It supplies the concrete commands, branch name, worktree convention, and known failure modes that the generic principles below resolve against. If the adapter is missing, that's your first STOP — onboard the repo (see the project's README install steps) before orchestrating.

**On the examples.** Worked examples are tagged *"Example (PublyApp/.NET):"*. They are **illustration, not law** — a sample from the project this playbook was first distilled from. On a different stack, read the principle and the example's *shape*, then resolve the specifics from your adapter.

---

## §1 — Discipline (non-negotiable guardrails)

These hold on every run, for every agent. They override speed, convenience, and your own judgment about "just this once."

1. **Orchestrate, don't execute.** Delegate every code edit, build, test run, PR, and issue mutation to an executor. The orchestrator's only hands-on work is decomposition, briefing, routing reviews, integration decisions, and talking to the human.
   *Why:* the orchestrator's value is holding the whole board in context; doing executor work inline burns that context and is where mistakes hide.

2. **Never commit or push to the main/default branch.** Work happens on feature branches off the default branch (name = adapter `default_branch`). The orchestrator never writes to it directly.
   *Why:* the default branch is the integration point of record; direct writes bypass review and break everyone downstream.

3. **Never merge without explicit, per-request human authorization.** A human says "merge X" each time. Prior approval of one merge never implies the next.
   *Why:* merging is the one irreversible, outward-facing step; it's the human's call, every time.

4. **The review loop is mandatory before integration, and the reviewer must be a different model family than the implementer.** Every executor result gets an independent review pass before it's integrated. The reviewer runs on a different provider/model family than produced the result (Codex implements → Claude reviews, and vice versa) — never review Codex output with Codex. Feed findings back until clean. Reviews must be rigorous: cover the full requirements coverage matrix plus affected-risk analysis — tests, performance, security, robustness, completeness, design patterns, code reuse (DRY), code elegance, and better-approach pressure while sticking to locked decisions/specs/plans.
   *Why:* an executor checking its own work is not a second opinion; cross-family review is what catches the plausible-but-wrong result. Same-family review also doubles that family's token spend on one task — the review re-ingests the full diff and coverage matrix on the same context window. Routing the review to the other family both sharpens the check and halves the per-family input cost.

5. **Effort ceiling — default high; xhigh requires ledgered escalation.** Cap executor/review effort at `high` by default. `xhigh` is allowed only with a ledgered escalation reason: final integration review, security/auth change, high-risk billing/data operation, architecture dispute, or pre-merge gate. Every xhigh use must be recorded in the run preflight ledger.
   *Why:* a deliberate cost/quality ceiling set by the human; `high` is the most capable tier in scope.

6. **Persist + link.** Squash bodies are written to the project `dump_dir` automatically; every PR carries a linked tracking issue (sub-issue of the relevant epic where one exists).
   *Why:* the issue tree is how the human tracks work; an unlinked PR or a lost squash body leaves the record incomplete.

7. **Subagents do not write durable notes directly.** Delegated subagents are vault-read-only by default. The orchestrator owns all durable documentation, session notes, logs, and knowledge-base writes. Subagents surface findings, corrections, and discoveries in their return payload — the orchestrator persists them. Emergency continuity handoffs are the only exception: a subagent may write a minimal handoff record before context/process loss, under a designated handoff directory.
   *Why:* multiple subagents writing independent notes fragments the record, duplicates effort, and creates drift. One orchestrator writing the coherent narrative prevents this.

8. **Dispatch only from a clean, named checkpoint.** Before each write-wave, the source branch/worktree state must be reproducible: committed, or explicitly stashed and described.
   *Why:* an uncommitted parent tree makes later waves impossible to separate cleanly, complicates rescue, and turns review/revert into guesswork.

9. **One captain per work board.** When a goal spans several clones/worktrees of the same product, run one hot orchestrator as the captain. The captain owns the queue, context, routing, review loop, and integration; executors are bounded one-shot packets launched into a target clone/worktree.
   *Why:* two or three full-context orchestrators on the same board duplicate the most expensive input. Speed comes from provider lanes and surgical bursts, not parallel captains re-reading the same world.

---

## §2 — The seven-phase loop

Each phase below states a portable **principle**, the **why**, a tagged **example**, and the **STOP-and-report triggers** that mean "halt and surface to the human rather than guess."

### 2.1 Decompose

**Principle.** Break the goal into independent, **file-disjoint** tasks — each completable, reviewable, and revertible on its own.
**Why.** Disjoint tasks parallelize without conflicts and fail independently; overlapping tasks serialize and entangle blame.
*Example (PublyApp/.NET):* the handler-contract drift triage split into 7 disjoint PRs (two renames, a file-split, a param reorder, a DTO rename, a validator refactor, a getter-cache fix) — each touching different files.
**STOP triggers:** two candidate tasks touch the same file → serialize them or merge into one task; a task can't be described without referencing another's output → they're not independent.

### 2.2 Decision-gate

**Principle.** Surface genuine judgment calls to the human *before* dispatching — choices that change scope or have no obvious default.
**Why.** Guessing on a scope-changing decision wastes a whole dispatch cycle; a 30-second question saves it.
*Example (PublyApp/.NET):* before the triage, asking the human to decide each drift item — rename file vs class, split vs allowlist the action-family, strict vs lenient parameter order.
**STOP triggers:** a decision changes what gets built and has no conventional default → ask; you're inventing a rationale to avoid asking → ask.

### 2.3 Dispatch

**Principle.** Run N executors concurrently, each in its **own isolated worktree**, each handed one **self-contained brief** (§3).
**Why.** Isolation prevents executors from colliding on the working tree; self-contained briefs keep an executor from needing context it doesn't have.
If the run must outlive the current orchestrator session (overnight work, disconnect-prone client, quota-reset wait), make it **durable**: materialize a run directory with prompts, reports, status markers, and a monitor entrypoint, then launch only as many concurrent executors as the adapter says the host can sustain.
For multi-clone work, start one captain from the adapter `captain_root` (usually the parent directory above sibling clones), keep a hot backlog of 3-5 ready packets, and dispatch bounded one-shot workers into `clone_roots`. Use provider lanes, not free-for-all sessions: each heavy Claude/Codex lane takes the next surgical packet that fits its role and current headroom; local/cheap lanes handle prep, tests, logs, summaries, and mechanical checks. Burst heavy concurrency only when packets are independently briefable (file-disjoint edits, cross-family reviews, competing design probes, or separated debugging probes).
**Name each worktree after the pull request it produces, never after the issue** (`pr<NUMBER>`, e.g. `pr994`). One issue routinely spawns several competing implementations, and the moment it does, issue-named directories collide and the human can no longer tell which tree holds which attempt. The PR number is also what a reviewer actually searches for. Because that number does not exist until the branch is pushed, create the worktree under a provisional slug, push and open the PR immediately, then `git worktree move` it onto its `pr<NUMBER>` name — the provisional window is minutes, not days.
*Example (PublyApp/.NET):* 7 parallel executor briefs, one per triage PR, each in `.worktrees/pr<NUMBER>`.
**STOP triggers:** more than one hot captain is steering the same board → collapse to one captain; concurrent **heavy-resource** jobs (Docker/e2e stacks, full builds/test suites) exceed host capacity → serialize those — agent *headcount* is not the cap, lightweight agents run many-in-parallel; a task isn't truly file-disjoint from a sibling in the same wave → re-decompose; the parent session may disappear before children finish and no durable monitor path exists → harden the run first.

### 2.4 Rescue

**Principle.** When an executor dies or hangs mid-task, **spawn a fresh executor to inherit its on-disk WIP and finish** — diagnose first, but never hand-fix the work yourself.
**Why.** Staying in the orchestrator seat preserves the discipline and the context boundary; hand-fixing is how "just this once" becomes the norm.
*Example (PublyApp/.NET):* an executor died during `pnpm install` after completing the .NET rename + client regen; a fresh executor was briefed with the exact WIP state (committed renames, pending verification) and finished the merge-ready PR.
**STOP triggers:** the WIP state on disk is ambiguous → inspect the worktree (status/log/diff) and write the findings into the inheritor's brief before dispatching; the executor died from a real code error (not an environment flake) → surface the error, don't just re-run.

### 2.5 Review loop

**Principle.** Every result gets an **independent review pass before integration**, ideally from a different model than produced it. Feed findings back; re-review until clean.
**Why.** The review is the safety net the discipline (§1.4) mandates. It catches latent bugs that pass all tests — the most dangerous kind.
When the human explicitly optimizes for latency, you may batch several low-risk edits into a **milestone** before running the review — but the review itself is still mandatory before integration/merge, and the full verification gate never becomes optional.
*Example (PublyApp/.NET):* a GPT review of the architecture-helper PR caught a `Contains("OpenApi")` substring exclusion that would have **silently dropped an authored type** from guard coverage with zero failing tests — fixed and spec-guarded before merge.
**STOP triggers:** review returns a blocking finding → fix-and-re-review, do not merge; review and executor disagree on whether something is real → get a second reviewer rather than averaging; a "skip review loops" instruction is being interpreted as "skip independent review before merge" → stop and correct the interpretation.

### 2.6 PR closure state machine (mandatory gate)

**Principle.** Pull-request closure is a mechanical state machine, not a prose claim. Every PR has exactly one derived state. Readiness is derived from evidence tied to the **same pushed commit**: clean worktree, local tip equals remote tip, required local gates green at that tip, GitHub CI green at that tip, a fresh independent review naming that exact tip with no blocking findings, every deferred finding backed by a verified issue, and verdict `APPROVED` or `APPROVED_WITH_FOLLOW_UPS`. Missing, stale, malformed, or contradictory evidence is `UNVERIFIED` — never success.

**States and their meaning (each PR has exactly one):**

| State | Meaning | Allowed next actions |
|---|---|---|
| `CI_RED` | A branch-caused required check is red. | Implement or verify a fix. No approval. |
| `CI_INFRA_RETRY` | A required check failed before testing code, with concrete infrastructure evidence. | Rerun the failed job within the adapter's retry budget. |
| `FIXING` | A bounded implementation packet owns named blocking findings. | Commit, push, and verify. |
| `LOCAL_VERIFY` | Work is pushed but the adapter's required local evidence is incomplete or stale. | Run the missing gate, one heavy command at a time. |
| `REVIEW_READY` | CI and local gates are green at the pushed tip. | Dispatch an independent adversarial review. |
| `REVIEWING` | One reviewer owns the exact pushed tip. | Wait for a structured verdict; do not edit the reviewed worktree. |
| `CHANGES_REQUIRED` | The review contains at least one blocking finding. | Create one fix packet accounting for every blocker. |
| `DESIGN_RESET` | The same root-cause class survived two attempted repair strategies. | Replace the mechanism or proof strategy, then verify and re-review. |
| `FOLLOW_UP_FILING` | No blockers remain, but follow-up findings lack issue IDs. | File and verify the issues. |
| `APPROVED_WITH_FOLLOW_UPS` | CI and local gates are green; review approved; every deferred finding has a verified issue. | Report ready and wait for owner merge authority. |
| `APPROVED` | CI and local gates are green; review approved with no required follow-ups. | Report ready and wait for owner merge authority. |
| `NEEDS_OWNER` | A genuine owner decision blocks progress. | Ask one narrow decision question. |
| `STALLED` | No qualifying progress within the adapter's time budget, or repeated executors died without producing evidence. | Rescue the lane, redesign the packet, or move to `NEEDS_OWNER`; another identical dispatch is forbidden. |
| `UNVERIFIED` | Required evidence is missing, malformed, stale, or contradictory. | Restore evidence; never infer a more favorable state. |

**Terminal states.** Only `APPROVED` and `APPROVED_WITH_FOLLOW_UPS` are terminal, and only a terminal state ends ownership of the PR, its packets, worktrees, and reviewer lanes. `NEEDS_OWNER` and `STALLED` pause or force rescue but never end ownership. A state-changing closure action may run only when the derived state equals the intended target — no prose claim can override a denied transition.

**Structured verdicts.** Reviewers return exactly one machine verdict: `CHANGES_REQUIRED` (at least one blocking finding), `APPROVED_WITH_FOLLOW_UPS` (no blockers; every follow-up finding has a verified issue), `APPROVED` (no blockers or required follow-ups), or `INCONCLUSIVE` (the reviewer could not prove the central claim or complete the required evidence). `INCONCLUSIVE` blocks approval without automatically accusing the code. An unknown schema version, unknown verdict, duplicate finding ID, missing follow-up issue ID, or malformed record is `UNVERIFIED`.

**Findings.** Each finding carries a severity (`CRITICAL`, `MAJOR`, `MEDIUM`, `MINOR`, `NOTE`) and a disposition (`BLOCKS_PR`, `FOLLOW_UP_ISSUE`, `NOTE_ONLY`). Only `BLOCKS_PR` blocks the PR; severity does not decide disposition by itself. `FOLLOW_UP_ISSUE` may leave the branch only after a real issue is filed, linked, and verified open or deliberately scheduled; verified follow-up findings are what lead to `APPROVED_WITH_FOLLOW_UPS`. `NOTE_ONLY` findings remain recorded notes and cannot reopen the loop.

**Mandatory blockers.** The following can never be deferred, whatever label the reviewer used — the gate promotes them to `BLOCKS_PR`:

- branch-caused red CI;
- an unmet acceptance requirement;
- a regression introduced or exposed by the branch;
- security, authorization, privacy, billing, or data-integrity uncertainty;
- failure or unverifiability of the PR's central claim;
- a guard that silently treats an undecidable input as safe;
- a test or verification mechanism that cannot detect the defect it claims to detect;
- unpushed work, dirty probe residue, or a reviewed-tip mismatch; and
- the same underlying blocker returning after an attempted fix.

**Central-claim rules.** Each project adapter declares `central_claim_rules`: claims that may never be deferred for that project. Reviewers must try to falsify the PR's central claim and provide both the failing escape and the legitimate control.

**Exact pushed-commit binding and stale approval.** All evidence — local verification, CI, review — binds to one exact pushed commit. Approval is invalidated immediately by any new commit, changed CI result, reopened blocker, missing follow-up issue, or mismatch between reviewed, local, remote, and CI tips. `INCONCLUSIVE`, unresolved evidence, and failed source lookups are loud refusals, never silent passes.

**Repeated-root-cause circuit breaker.** Each blocking finding carries a normalized `root_cause`. If the same root cause survives two attempted repairs, the PR enters `DESIGN_RESET`: no further syntax-instance patch; instead require a mechanism-level explanation of the wrong default or missing invariant, a structural or mechanical control that prevents bypass, paired proof with a mutation that disables the control, and a fresh full adversarial review. This is not a review cap and cannot be used to defer the blocker.

**Stagnation and retry escalation.** Activity is not progress: a running process, another review number, or another dispatch does not reset the stagnation clock. No qualifying progress within the adapter's `stagnation_budget_minutes`, or two executors dying on the same packet without valid evidence, enters `STALLED`. Proven infrastructure failures may be rerun only within the adapter's `infra_retry_budget`; exhausting it enters `NEEDS_OWNER`. Heavy verification (full builds, e2e, API suites) runs under an exclusive heavy-job lease so two expensive suites never overlap.

**Sources of truth and projections.** Authoritative, in order: Git worktree and remote refs; GitHub PR metadata and CI; durable local verification records; durable structured review records; GitHub follow-up issues. Trello, dashboards, chat summaries, and notifications are derived projections and are never approval evidence. Projection writes are refused only when required sources are unavailable, malformed, or contradictory; valid intermediate states are projected normally. A projection failure never mutates authoritative closure evidence.

**The mechanical gate.** One reusable command ships in this repo:

```bash
pr-closure status --config <project-closure.json> --pr <N>
pr-closure check-transition --config <project-closure.json> --pr <N> --to <STATE>
pr-closure sync --config <project-closure.json> --pr <N> [--apply]
```

`pr-closure check-transition` is a **mandatory precondition** before every state-changing closure action (dispatch, fix, rerun, review, follow-up filing, projection apply, ready report). A denied transition stops the action. Missing evidence and tool/API failures are non-zero exits — fail closed, never infer a favorable state. Exit codes are stable: `0` read/check succeeded, `2` invalid input, `3` source unavailable or malformed, `4` transition denied, `5` verification/projection command failed, `6` heavy-job lease unavailable. Evidence lives in a durable run directory outside temporary session folders; the run's `state.json` is a cache, never the authority. Empty, undersized, or markerless lane output is failure even with exit 0.

**Model policy (all projects).** Implementation defaults to DeepSeek V4 Flash. Independent review defaults to GPT-5.6 Luna at `xhigh` reasoning effort. No new Claude implementation, review, or coordination calls. Historical Claude artifacts remain valid evidence when they already satisfy the structured cross-family contract; they are not rerun solely because the default changed. The reviewer family must differ from the implementer family — an OpenAI-family reviewer may review a DeepSeek implementation, but never an OpenAI-family implementation.

**Adversarial review is preserved.** The gate reduces wasted cycles, not review pressure: every `REVIEW_READY` commit gets a fresh independent cross-family review, every fix is re-reviewed once CI and local gates are green, and there is no maximum review count.

**Why.** Reminders did not stop the paid failure modes: reviewing red CI, reviewing unpushed commits, losing verdicts under temporary session directories, milestone-as-approval, and status from memory while GitHub, Trello, and the worktree disagree. Deriving state from sources of truth and refusing invalid transitions makes progress and approval machine-checkable.

**STOP triggers:** `check-transition` denies the requested transition → stop; the derived state governs. Required evidence is missing, stale, or contradictory → `UNVERIFIED`; restore evidence, never infer. The same root cause returns after a second repair → `DESIGN_RESET`; no instance patch. No qualifying progress within the stagnation budget, or repeated executor deaths → `STALLED`; rescue with a changed strategy, never repeat the identical dispatch. An adapter lacks closure fields → preflight STOP.

### 2.7 Merge dance

**Principle.** Integrate in a fixed order: pre-flight the PR (tolerate transient `UNKNOWN` state with bounded retries) → rebase only if needed (additive resolution for known shared files; STOP on anything else) → rerun the adapter's **full acceptance gate** after the rebase, not just touched tests → **remove the worktree before deleting the branch** → squash-merge with the body persisted to `dump_dir` → sync the default branch → repeat for the next PR.
**Why.** The order is load-bearing: a live worktree blocks branch deletion; an un-synced default branch makes the next PR's pre-flight lie; a non-additive conflict resolved by guessing corrupts the merge; and a rebase can silently import same-surface changes that only the full suite exposes.
*Example (PublyApp/.NET):* a four-PR sequence stalled when `--delete-branch` failed against a still-checked-out worktree; the fix was to remove the worktree *first*, then merge — and to retry the GitHub `UNKNOWN/UNKNOWN` pre-flight state with short sleeps rather than treating it as a failure.
**STOP triggers:** a conflict appears in a file not on the adapter's `additive_merge_files` list → report, don't guess; pre-flight stays `UNKNOWN` after bounded retries → report; the rebase pulled in a newly-landed same-surface feature or hard-rule obligation that changes scope → surface the expansion before proceeding; the human hasn't authorized this specific merge → halt (see §1.3).

### 2.8 Close-out

**Principle.** After merges, reconcile the issue tree: link children as native sub-issues, apply `Refs` vs `Closes` deliberately, and close issues (with a wrap-up comment) where policy requires a manual close.
**Why.** The issue tree is the human's map of the work; a PR that should have closed its issue but didn't, or an unlinked child, leaves the map wrong.
*Example (PublyApp/.NET):* the triage epic was closed *manually* with a wrap-up comment because all its sub-PRs used `Refs` (not `Closes`) by policy; a separate helper issue auto-closed because its PR used `Closes`.
**STOP triggers:** unsure whether a PR should auto-close its issue → default to `Refs` + manual close; a child issue isn't linked to its parent → link it via the platform's sub-issue API before closing anything.

---

## §3 — Executor brief contract

A dispatch brief must be **self-contained**: an executor with no prior context should be able to act correctly from it alone. Use this skeleton; fill placeholders from the adapter (§4).

The captain keeps a hot backlog of the next 3-5 packets. A packet is ready only when it names the lane, target clone/worktree, exact files or symbols, expected artifact, validation command, and STOP conditions. If the packet needs broad history or repo exploration, it is not ready for parallel dispatch.

**Skeleton (required elements):**

1. **Header** — packet id, provider lane, target clone/worktree, execution mode + effort. Effort ≤ `high` by default; `xhigh` requires a ledgered escalation reason (see §1.5). *(Resolve executor + effort from adapter `executor` / `provider_lanes`.)*
2. **Context sourcing** — the orchestrator discovers vault, playbook, adapter, and skills ONCE, then passes distilled context to each subagent. Subagents do not re-run proactive loading.
3. **Checkpoint state** — the brief states the exact starting checkpoint (branch/head commit, or explicit stash/WIP note) so the executor is writing from a named baseline.
4. **Absolute-path discipline** — every version-control command uses an **absolute** repo/worktree path, never a bare relative path.
5. **Worktree setup** — create/locate the isolated worktree at adapter `worktree_root`.
6. **Required reading** — the exact files the executor must read before acting (so it follows existing patterns instead of inventing).
7. **The work** — the concrete change, scoped to file-disjoint targets.
8. **Verification** — the project's **setup step first** (adapter `setup_cmd`), then the normal targeted gates (`build_cmd` / `test_cmd` / `lint_cmd`), and the adapter's **full acceptance gate** (`acceptance_cmd`) whenever the change is broad, mechanical, generated, or rebased. State expected outcomes.
9. **Guard path** — if the repo relies on hooks, CI checks, or soft gates, the brief names the **actual enforcement path** from the adapter (`push_guard`), not a guessed one (for example: active `core.hooksPath`, required workflow, or "soft gate only").
10. **Commit + PR** — a **pre-written commit message and PR body** (don't make the executor compose them), and the explicit `Refs #NNN` vs `Closes #NNN` choice.
11. **Continuity plan** — if quota/rate-limit or session-loss is plausible, include the adapter's fallback/model ladder and whether the run must be durable. When multiple viable model/provider families are available, the orchestrator should pick from that ladder automatically rather than requiring repeated human routing, and should spread heavy execution/review load across the available families when practical. Use task fit and adapter policy as tiebreakers; escalate to the human only when a specific named provider/model is truly required or the available routes are ambiguous/unusable.
12. **STOP-and-report escape hatches** — the specific conditions under which the executor must halt and report rather than guess (non-additive conflict, unexpected build error, scope surprise).
13. **Constraints block** — never push/commit the default branch; never merge; `--force-with-lease` only (never plain `--force`); `--no-verify` only on a feature-branch force-push; effort ceiling.

*Example (PublyApp/.NET) — a single-PR brief, abbreviated:*

> `--effort high --write --no-sandbox`
> Create worktree `…/.claude/worktrees/538a-rename` off `origin/develop`. Use `git -C "<absolute-worktree-path>"` for every git command.
> **Read first:** `apps/api/Modules/Auth/Handlers/PassWordLogin.cs` (confirm class is already `PasswordLogin`).
> **Work:** rename the file to `PasswordLogin.cs` (two-step temp rename — Windows is case-insensitive: `→ _tmp.cs → PasswordLogin.cs`, commit between).
> **Verify:** `dotnet restore` (fresh worktree) → `just build-api` (expect 0/0) → arch spec filter (expect pass).
> **Commit/PR:** message + body pre-written below; PR body ends with `Refs #538` (NOT `Closes` — epic closes manually).
> **STOP if:** the rename surfaces references beyond the file itself, or build fails for any reason other than missing restore.
> **Constraints:** never push develop; never merge; `--force-with-lease` only; `--no-verify` only on the feature-branch force-push; effort ≤ high.

---

## §4 — Adapter contract

Every repo supplies `<repo>/.ai/orchestration-adapter.md` as **fielded descriptive data** (not prose), so every agent parses it identically. Required fields:

| Field | Meaning |
|---|---|
| `default_branch` | The protected main/integration branch. Never pushed or committed directly (§1.2). |
| `setup_cmd` | Fresh-worktree bootstrap command(s): restore/install/sync before any build or test. |
| `build_cmd` | Command that builds the project. |
| `test_cmd` | Command(s) that run the relevant tests (with filtering syntax if applicable). |
| `lint_cmd` | Lint / type-check / format-check command(s). |
| `acceptance_cmd` | The full post-rebase / pre-merge gate. This is the command that catches same-surface integration failures a touched-suite can miss. |
| `client_regen_cmd` | API-client (or other generated-artifact) regeneration command, or `none`. |
| `worktree_root` | Path convention for isolated worktrees, **including the naming rule**. Name a worktree after the pull request it produces — `pr<NUMBER>` — never after the issue. One issue routinely spawns several competing implementations, and issue-named directories collide the moment it does; the PR number is also what a reviewer searches for. Since the number does not exist until the branch is pushed, create under a provisional slug, open the PR immediately, then `git worktree move` onto the final name. Never construct a worktree path by string-building from the convention — read it from `git worktree list --porcelain`, which stays correct after a rename. |
| `captain_root` | Directory from which one captain can coordinate this repo and any sibling clones. Use `repo root` for a single-clone setup. |
| `clone_roots` | Known sibling clone/worktree roots the captain may dispatch into, or `none` for a single clone. |
| `host_parallelism` | Safe concurrency ceiling / batching rule for this host and repo (especially when builds/tests are heavy). |
| `executor` | Which executor to dispatch + its default effort (≤ `high`). |
| `model_ladder` | Preferred fallback order when the primary executor/model rate-limits or hits quota, including any approved cross-family alternates so the orchestrator can route automatically without repeatedly asking the human. |
| `provider_lanes` | Approved DeepSeek/OpenAI/local lanes, their default roles, and which lane owns review/fix/design/verification packets. |
| `hot_backlog` | Number of pre-shaped packets the captain should keep ready (default 3-5), plus where packet/board files live if durable. |
| `packet_template` | Path to the repo's packet template, or `~/ai-orchestration-playbook/captain-packet-template.md`. |
| `push_guard` | The real enforcement path for push/merge policy (active hook path, CI gate, soft gate, or `none`). |
| `known_quirks` | **Highest-value field.** Hard-won host/tooling failure modes + their fixes. |
| `additive_merge_files` | Files whose merge conflicts are resolved *additively* (keep both sides); anything else → STOP. |
| `dump_dir` | Where squash bodies and working artifacts are written. |
| `issue_hierarchy` | Epic structure + the sub-issue linking convention/command. |

PR-opening repos must also supply the closure fields from §2.6 (see `adapter-template.md`); an adapter that lacks them is a preflight STOP for orchestrated PR work.

**Discoverability (dual).** (a) An orchestrating agent reads `<repo>/.ai/orchestration-adapter.md` at the start of every run; **and** (b) each repo adds a one-line pointer to that file under an **"AI Orchestration"** heading in its `AGENTS.md`, so even an agent that doesn't know the `.ai/` convention finds it.

*Example (PublyApp/.NET) adapter values:*

| Field | Value |
|---|---|
| `default_branch` | `develop` |
| `setup_cmd` | `dotnet restore`; `pnpm install --frozen-lockfile` |
| `build_cmd` | `just build-api` |
| `test_cmd` | `just test-analyzers`; filtered `dotnet test apps/api/Tests/PublyApp.Api.Tests.csproj -c Test --filter "FullyQualifiedName~<X>"` |
| `lint_cmd` | `pnpm lint`; `just tsc-front` |
| `acceptance_cmd` | `just build-api`; `just test-analyzers`; `pnpm lint`; `just tsc-front` |
| `client_regen_cmd` | `just generate-client` |
| `worktree_root` | `.worktrees/pr<NUMBER>` (e.g. `.worktrees/pr994`), inside the clone — never a sibling of it |
| `captain_root` | repo parent when coordinating sibling clones; otherwise repo root |
| `clone_roots` | sibling local clones/worktrees approved by the adapter, or `none` |
| `host_parallelism` | at most 3 concurrent executor waves; never run multiple heavy `dotnet` / `pnpm` verification jobs at once |
| `executor` | `codex:codex-rescue` @ effort `high` |
| `model_ladder` | primary `codex:codex-rescue` @ `high`; on quota/rate-limit fall back per repo policy to the next approved executor without changing the orchestration contract |
| `provider_lanes` | DeepSeek V4 Flash lane for implementation; GPT-5.6 Luna `xhigh` lane for independent review; local lane for grep/log/test prep |
| `hot_backlog` | keep 3-5 ready packets in the run `dump_dir`; do not launch broad exploratory packets |
| `packet_template` | `~/ai-orchestration-playbook/captain-packet-template.md` |
| `push_guard` | active hook path is Husky (`core.hooksPath=.husky/_`); `.husky/pre-push` blocks direct pushes to `develop`; feature-branch policies beyond that are soft/brief-driven unless CI says otherwise |
| `known_quirks` | sticky working-directory → use absolute paths for every `git -C` and `--body-file`; a fresh worktree needs `dotnet restore` before `just build-api` (recipe uses `--no-restore`); `pnpm` OOMs on the lint-ts test suite → verify lint locally, don't block on it; GitHub `--delete-branch` fails while the branch's worktree is still checked out → remove worktree first; after rebases, rerun the full acceptance gate, not just touched tests |
| `additive_merge_files` | `.oxlintrc.json`, `.editorconfig`, `AGENTS.md`, `packages/lint-ts/src/index.js`, `docs/guides/lint-rules.md` |
| `dump_dir` | `.dump/` |
| `issue_hierarchy` | epics (e.g. lint framework, arch guards, handler contract) + native sub-issues via `gh api --method POST repos/<owner>/<repo>/issues/<PARENT>/sub_issues -F sub_issue_id=<childDbId>` |

---

## §5 — Token discipline

Portable token-saving tactics, independent of which agent loads them. Each tactic states its trigger, enforcement point, and per-run evidence requirement.

**Cost reality — optimize input, not reasoning.** On a real run, ~99% of an executor's token spend is **input/context** (briefs, injected instruction files, required-reading, re-ingested diffs); output and reasoning tokens are typically <1% combined. Effort tiering and the xhigh ceiling (§5.1, §1.5) cap that <1% — necessary for quality control, but they do **not** move the bill. Token savings come from cutting input volume: instruction-file size (§5.5), cross-family review routing (§5.6), brief distillation (§3.2), and not re-shipping context on retries.

### 5.1 Model tiering by task value
Decomposition, planning, spec review, and routine dispatch use fast/cheap models. Reserve `xhigh` for final integration review, high-risk/security/auth changes, architectural disputes, and pre-merge gates only. Every xhigh use requires a ledgered escalation reason (see §6). *(This controls quality and the <1% reasoning slice, not the input bill — see Cost reality above.)*

### 5.2 Targeted verification
Per-task inner loops use focused test runs (targeted files, --last-failed, smoke checks). The full acceptance gate runs once after rebase (rebase can invalidate per-task results). These are sequential gates, not alternatives. Neither skips the other.

### 5.3 Milestone batching
Batch low-risk edits into a milestone before running verification, rather than running the full gate after every sub-task.

### 5.4 Installed tooling
| Tool | What it does | Usage |
|------|-------------|-------|
| RTK | Compresses noisy shell output (test logs, git status, build traces) before it enters context | Prefix command: `rtk git ...`, `rtk test ...`, `rtk grep ...` |
| CodeGraph | Queries a code graph index — skip whole-file reads in large repos | `codegraph explore "question"` before grep; build index with `codegraph init` |
| Context-Mode | Routes heavy tool output through a sandbox, returns only the summary | Available on demand; do not auto-inject its plugin unless a run will actually use `ctx_batch_execute`, `ctx_execute_file`, or `ctx_search` |
| Caveman | Terse prose (note: benchmarks show +7% tokens, +3% cost) | Dormant — do not invoke |
| Ponytail | 7-rung lazy-senior-dev ladder before writing code (-54% LOC, -22% tokens) | Available on demand; do not auto-inject globally. Use lite/full/ultra only when the task benefits from the ladder |

Tools report three states: `missing` (not installed), `available` (installed, ready), `active` (used this run). Only `active` counts toward token optimization in the preflight ledger.

### 5.5 Context hygiene
- Keep system/project files (CLAUDE.md, AGENTS.md) under 1KB each — invariants only. These inject on **every** turn, so a fat instruction file is a fixed multiplier on the whole session. Don't restate what a hook, MCP server, or the playbook already injects; point to it instead. Re-measure after edits (`wc -c`); an 11KB AGENTS.md is ~10x its budget.
- Keep agent plugin/skill/tool surfaces lean by default. Enable only the plugin/skill/toolsets needed for the current lane; park heavy narrative/style plugins, broad skill banks, browser/media tools, and delegation schemas unless the task explicitly needs them. A useful default is: memory + skills + file + terminal + web/search + code execution + session search + cron/todo/clarify; add browser/vision/image/audio/delegation only for that run.
- Default interactive/routine agents to cheap models and low/medium reasoning; expose named `*-high` and `*-xhigh` escalation lanes for deliberate use. If a one-line/status prompt can burn premium-window percentage points, the default lane is wrong.
- Re-measure prompt surfaces after config changes: Codex `codex debug prompt-input 'status'` byte count; Hermes `hermes tools list` plus config/toolset inspection. Record before/after in the ledger or closeout note.
- Use surgical file context — reference specific files and functions, not full repos.
- Start fresh (/clear, /new) between unrelated tasks. Long sessions compound costs exponentially.
- Disconnect unused MCP servers — each adds thousands of tokens per message in tool definitions.
- Never re-ship full context on retry. On quota/rate-limit (429) or a flaked dispatch, stop and re-route — do **not** resend the entire brief on a fixed retry cycle. A retry storm that re-ingests the brief every N seconds is pure wasted input; kill the executor/broker rather than letting it loop.

### 5.6 Cross-family review routing
The mandatory review (§1.4) runs on a **different model family than the implementer**. This is a token tactic as much as a quality one: reviewing Codex output with Codex makes one task two full-context Codex passes (the review re-ingests the diff + the rigorous coverage matrix). Routing the review to the other family halves the per-family input load on the heavy path and gives a genuinely independent check. Record the implementer/reviewer route split in the preflight ledger (§6) — a row where both are the same family is a STOP-and-reconsider, not a dispatch.

### 5.7 Parallel-session discipline
When the human would otherwise open 2-3 orchestration sessions for sibling clones, use the captain/lane model instead (§1.9, §2.3). Keep one hot captain and launch bounded one-shot packets into clones. Track waste as **fresh input per completed packet**, not raw activity: if multiple packets re-ship the same broad context, stop and distill the stable prefix once before launching more. Do not throttle useful independent packets just because they are parallel; throttle duplicate context and heavy-resource jobs.

## §6 — Preflight dispatch ledger

Before dispatching any executor or subagent, append one JSONL preflight row to the run ledger. The ledger is the executable dispatch gate that unifies route choice, quota, risk, required checks, token-tool evidence, bypass safety, and source-of-truth conflict detection.

**Format:** Append-only JSONL at `<orchestration_home>/orchestration/ledger/YYYY-MM-DD.jsonl`. One row per dispatch. Created before any subagent is dispatched.

**Schema (required fields — unknown = stop):**

```
run_id, timestamp
captain: {session_id, board_dir, packet_id, lane: claude|codex|local|other, target_clone, hot_backlog_size}
task_risk: {level: low|medium|high|critical, reasons: []}
scope: {repo, worktree, branch, dirty_state}
routes: {implementer: {provider, model, quota_signal}, reviewer: {provider, model, quota_signal}, fallbacks: []}
context_budget: {packet_size: tiny|small|medium|large, stable_prefix_reused: bool, fresh_input_estimate: low|medium|high, duplicate_context_risk: low|medium|high}
required: [{name, check, status: pass|fail|unknown, failure_action: stop|degraded|ask}]
token_tools: {rtk, codegraph, context-mode, ponytail: active|available|missing}
bypass: {codex_bypass: bool, claude_bypass: bool, invariant_asserted: bool}
verification: {tier: targeted|milestone|full}
decision: dispatch|degraded|stop
```

**Rule:** No dispatch until a valid row exists with `decision: dispatch`. If any mandatory field is unknown, safety/scope checks fail, or required items have no declared failure_action, the decision defaults to `stop` — not optimism. The concrete path (`~/.hermes/...` etc.) is specified by the agent's adapter or global config.

### Two paths

1. **LLM-orchestrator path** (interactive dispatch): the captain writes the rich row (`captain`, risk, routes, `context_budget`, required[], token_tools) from its own reasoning before dispatching subagents. This is a true gate — `decision: stop` blocks dispatch. Enforced by the packet/brief contract (§3) and the orchestrator's own discipline.

2. **Mechanical dispatch path** (autonomous/kanban workers): a pre-spawn helper writes a minimal observational row with `decision: log-only`. The mechanical path cannot compute `required[]`, `token_tools.active`, `task_risk`, or nuanced `context_budget` — those fields are the LLM captain's responsibility. This path is a **log**, not a gate. The gate for autonomous work lives upstream: pre-spawn guards, profile concurrency caps, and stale-timeout detection.
