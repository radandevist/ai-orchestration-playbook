# AI Orchestration Playbook

> The portable operating model for an **orchestrating agent** running a batch of work through executors. Read this top-to-bottom before starting an orchestration run, then bind it to the current project via that repo's adapter (see §4).

## §0 — Preamble

**What this is.** A single, self-contained procedure for orchestrating multi-task software work: decompose a goal, dispatch parallel executors in isolated worktrees, review their output, integrate it, and close out the tracking issues — without the orchestrator ever executing project code itself.

**Who reads it.** Any orchestrating agent — Claude, Hermes, or a future agent. The document is agent-neutral. Agent-specific quirks belong in that agent's own global instructions, never here.

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

4. **The review loop is mandatory before integration.** Every executor result gets an independent review pass (ideally cross-model) before it's integrated. Feed findings back until clean.
   *Why:* an executor checking its own work is not a second opinion; the review loop is what catches the plausible-but-wrong result. Skipping it is the most expensive shortcut there is.

5. **Effort ceiling = high; never xhigh.** Cap executor/review effort at `high`. `xhigh` is banned.
   *Why:* a deliberate cost/quality ceiling set by the human; `high` is the most capable tier in scope.

6. **Persist + link.** Squash bodies are written to the project `dump_dir` automatically; every PR carries a linked tracking issue (sub-issue of the relevant epic where one exists).
   *Why:* the issue tree is how the human tracks work; an unlinked PR or a lost squash body leaves the record incomplete.

7. **Dispatch only from a clean, named checkpoint.** Before each write-wave, the source branch/worktree state must be reproducible: committed, or explicitly stashed and described.
   *Why:* an uncommitted parent tree makes later waves impossible to separate cleanly, complicates rescue, and turns review/revert into guesswork.

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
*Example (PublyApp/.NET):* 7 parallel executor briefs, one per triage PR, each in `.claude/worktrees/<short-name>`.
**STOP triggers:** concurrent count exceeds host capacity → batch in waves; a task isn't truly file-disjoint from a sibling in the same wave → re-decompose; the parent session may disappear before children finish and no durable monitor path exists → harden the run first.

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

### 2.6 Merge dance

**Principle.** Integrate in a fixed order: pre-flight the PR (tolerate transient `UNKNOWN` state with bounded retries) → rebase only if needed (additive resolution for known shared files; STOP on anything else) → rerun the adapter's **full acceptance gate** after the rebase, not just touched tests → **remove the worktree before deleting the branch** → squash-merge with the body persisted to `dump_dir` → sync the default branch → repeat for the next PR.
**Why.** The order is load-bearing: a live worktree blocks branch deletion; an un-synced default branch makes the next PR's pre-flight lie; a non-additive conflict resolved by guessing corrupts the merge; and a rebase can silently import same-surface changes that only the full suite exposes.
*Example (PublyApp/.NET):* a four-PR sequence stalled when `--delete-branch` failed against a still-checked-out worktree; the fix was to remove the worktree *first*, then merge — and to retry the GitHub `UNKNOWN/UNKNOWN` pre-flight state with short sleeps rather than treating it as a failure.
**STOP triggers:** a conflict appears in a file not on the adapter's `additive_merge_files` list → report, don't guess; pre-flight stays `UNKNOWN` after bounded retries → report; the rebase pulled in a newly-landed same-surface feature or hard-rule obligation that changes scope → surface the expansion before proceeding; the human hasn't authorized this specific merge → halt (see §1.3).

### 2.7 Close-out

**Principle.** After merges, reconcile the issue tree: link children as native sub-issues, apply `Refs` vs `Closes` deliberately, and close issues (with a wrap-up comment) where policy requires a manual close.
**Why.** The issue tree is the human's map of the work; a PR that should have closed its issue but didn't, or an unlinked child, leaves the map wrong.
*Example (PublyApp/.NET):* the triage epic was closed *manually* with a wrap-up comment because all its sub-PRs used `Refs` (not `Closes`) by policy; a separate helper issue auto-closed because its PR used `Closes`.
**STOP triggers:** unsure whether a PR should auto-close its issue → default to `Refs` + manual close; a child issue isn't linked to its parent → link it via the platform's sub-issue API before closing anything.

---

## §3 — Executor brief contract

A dispatch brief must be **self-contained**: an executor with no prior context should be able to act correctly from it alone. Use this skeleton; fill placeholders from the adapter (§4).

**Skeleton (required elements):**

1. **Header** — execution mode + effort. Effort ≤ `high` (never `xhigh`; §1.5). *(Resolve executor + effort from adapter `executor`.)*
2. **Checkpoint state** — the brief states the exact starting checkpoint (branch/head commit, or explicit stash/WIP note) so the executor is writing from a named baseline.
3. **Absolute-path discipline** — every version-control command uses an **absolute** repo/worktree path, never a bare relative path.
   *Why:* a stuck or surprising working directory silently misroutes commands (e.g. writes landing in the wrong tree, or a `--body-file` reading from nowhere). Absolute paths are immune.
4. **Worktree setup** — create/locate the isolated worktree at adapter `worktree_root`.
5. **Required reading** — the exact files the executor must read before acting (so it follows existing patterns instead of inventing).
6. **The work** — the concrete change, scoped to file-disjoint targets.
7. **Verification** — the project's **setup step first** (adapter `setup_cmd`), then the normal targeted gates (`build_cmd` / `test_cmd` / `lint_cmd`), and the adapter's **full acceptance gate** (`acceptance_cmd`) whenever the change is broad, mechanical, generated, or rebased. State expected outcomes.
8. **Guard path** — if the repo relies on hooks, CI checks, or soft gates, the brief names the **actual enforcement path** from the adapter (`push_guard`), not a guessed one (for example: active `core.hooksPath`, required workflow, or "soft gate only").
9. **Commit + PR** — a **pre-written commit message and PR body** (don't make the executor compose them), and the explicit `Refs #NNN` vs `Closes #NNN` choice.
10. **Continuity plan** — if quota/rate-limit or session-loss is plausible, include the adapter's fallback/model ladder and whether the run must be durable.
11. **STOP-and-report escape hatches** — the specific conditions under which the executor must halt and report rather than guess (non-additive conflict, unexpected build error, scope surprise).
12. **Constraints block** — never push/commit the default branch; never merge; `--force-with-lease` only (never plain `--force`); `--no-verify` only on a feature-branch force-push; effort ceiling.

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
| `worktree_root` | Path convention for isolated worktrees (e.g. `.claude/worktrees/<short-name>`). |
| `host_parallelism` | Safe concurrency ceiling / batching rule for this host and repo (especially when builds/tests are heavy). |
| `executor` | Which executor to dispatch + its default effort (≤ `high`). |
| `model_ladder` | Preferred fallback order when the primary executor/model rate-limits or hits quota. |
| `push_guard` | The real enforcement path for push/merge policy (active hook path, CI gate, soft gate, or `none`). |
| `known_quirks` | **Highest-value field.** Hard-won host/tooling failure modes + their fixes. |
| `additive_merge_files` | Files whose merge conflicts are resolved *additively* (keep both sides); anything else → STOP. |
| `dump_dir` | Where squash bodies and working artifacts are written. |
| `issue_hierarchy` | Epic structure + the sub-issue linking convention/command. |

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
| `worktree_root` | `.claude/worktrees/<short-name>` |
| `host_parallelism` | at most 3 concurrent executor waves; never run multiple heavy `dotnet` / `pnpm` verification jobs at once |
| `executor` | `codex:codex-rescue` @ effort `high` |
| `model_ladder` | primary `codex:codex-rescue` @ `high`; on quota/rate-limit fall back per repo policy to the next approved executor without changing the orchestration contract |
| `push_guard` | active hook path is Husky (`core.hooksPath=.husky/_`); `.husky/pre-push` blocks direct pushes to `develop`; feature-branch policies beyond that are soft/brief-driven unless CI says otherwise |
| `known_quirks` | sticky working-directory → use absolute paths for every `git -C` and `--body-file`; a fresh worktree needs `dotnet restore` before `just build-api` (recipe uses `--no-restore`); `pnpm` OOMs on the lint-ts test suite → verify lint locally, don't block on it; GitHub `--delete-branch` fails while the branch's worktree is still checked out → remove worktree first; after rebases, rerun the full acceptance gate, not just touched tests |
| `additive_merge_files` | `.oxlintrc.json`, `.editorconfig`, `AGENTS.md`, `packages/lint-ts/src/index.js`, `docs/guides/lint-rules.md` |
| `dump_dir` | `.dump/` |
| `issue_hierarchy` | epics (e.g. lint framework, arch guards, handler contract) + native sub-issues via `gh api --method POST repos/<owner>/<repo>/issues/<PARENT>/sub_issues -F sub_issue_id=<childDbId>` |
