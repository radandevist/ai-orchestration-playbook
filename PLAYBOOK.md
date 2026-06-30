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

1. **Header** — execution mode + effort. Effort ≤ `high` by default; `xhigh` requires a ledgered escalation reason (see §1.5). *(Resolve executor + effort from adapter `executor`.)*
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
| `worktree_root` | Path convention for isolated worktrees (e.g. `.claude/worktrees/<short-name>`). |
| `host_parallelism` | Safe concurrency ceiling / batching rule for this host and repo (especially when builds/tests are heavy). |
| `executor` | Which executor to dispatch + its default effort (≤ `high`). |
| `model_ladder` | Preferred fallback order when the primary executor/model rate-limits or hits quota, including any approved cross-family alternates so the orchestrator can route automatically without repeatedly asking the human. |
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

---

## §5 — Token discipline

Portable token-saving tactics, independent of which agent loads them. Each tactic states its trigger, enforcement point, and per-run evidence requirement.

**Cost reality — optimize input, not reasoning.** On a real run, ~99% of an executor's token spend is **input/context** (briefs, injected instruction files, required-reading, re-ingested diffs); output and reasoning tokens are typically <1% combined. Effort tiering and the xhigh ceiling (§5.1, §1.5) cap that <1% — necessary for quality control, but they do **not** move the bill. Token savings come from cutting input volume: instruction-file size (§5.5), cross-family review routing (§5.6), brief distillation (§3.2), and not re-shipping context on retries.

### 5.1 Model tiering by task value
Decomposition, planning, spec review, and routine dispatch use fast/cheap models. Reserve expensive models (xhigh, Opus) for final integration review, high-risk/security/auth changes, architectural disputes, and pre-merge gates only. Every xhigh use requires a ledgered escalation reason (see §6). *(This controls quality and the <1% reasoning slice, not the input bill — see Cost reality above.)*

### 5.2 Targeted verification
Per-task inner loops use focused test runs (targeted files, --last-failed, smoke checks). The full acceptance gate runs once after rebase (rebase can invalidate per-task results). These are sequential gates, not alternatives. Neither skips the other.

### 5.3 Milestone batching
Batch low-risk edits into a milestone before running verification, rather than running the full gate after every sub-task.

### 5.4 Installed tooling
| Tool | What it does | Usage |
|------|-------------|-------|
| RTK | Compresses noisy shell output (test logs, git status, build traces) before it enters context | Prefix command: `rtk git ...`, `rtk test ...`, `rtk grep ...` |
| CodeGraph | Queries a code graph index — skip whole-file reads in large repos | `codegraph explore "question"` before grep; build index with `codegraph init` |
| Context-Mode | Routes heavy tool output through a sandbox, returns only the summary | `ctx_batch_execute`, `ctx_execute_file`, `ctx_search` |
| Caveman | Terse prose (note: benchmarks show +7% tokens, +3% cost) | Dormant — do not invoke |
| Ponytail | 7-rung lazy-senior-dev ladder before writing code (-54% LOC, -22% tokens) | Active on all agents. Levels: lite/full/ultra/off |

Tools report three states: `missing` (not installed), `available` (installed, ready), `active` (used this run). Only `active` counts toward token optimization in the preflight ledger.

### 5.5 Context hygiene
- Keep system/project files (CLAUDE.md, AGENTS.md) under 1KB each — invariants only. These inject on **every** turn, so a fat instruction file is a fixed multiplier on the whole session. Don't restate what a hook, MCP server, or the playbook already injects; point to it instead. Re-measure after edits (`wc -c`); an 11KB AGENTS.md is ~10x its budget.
- Use surgical file context — reference specific files and functions, not full repos.
- Start fresh (/clear, /new) between unrelated tasks. Long sessions compound costs exponentially.
- Disconnect unused MCP servers — each adds thousands of tokens per message in tool definitions.
- Never re-ship full context on retry. On quota/rate-limit (429) or a flaked dispatch, stop and re-route — do **not** resend the entire brief on a fixed retry cycle. A retry storm that re-ingests the brief every N seconds is pure wasted input; kill the executor/broker rather than letting it loop.

### 5.6 Cross-family review routing
The mandatory review (§1.4) runs on a **different model family than the implementer**. This is a token tactic as much as a quality one: reviewing Codex output with Codex makes one task two full-context Codex passes (the review re-ingests the diff + the rigorous coverage matrix). Routing the review to the other family halves the per-family input load on the heavy path and gives a genuinely independent check. Record the implementer/reviewer route split in the preflight ledger (§6) — a row where both are the same family is a STOP-and-reconsider, not a dispatch.

## §6 — Preflight dispatch ledger

Before dispatching any executor or subagent, append one JSONL preflight row to the run ledger. The ledger is the executable dispatch gate that unifies route choice, quota, risk, required checks, token-tool evidence, bypass safety, and source-of-truth conflict detection.

**Format:** Append-only JSONL at `<orchestration_home>/orchestration/ledger/YYYY-MM-DD.jsonl`. One row per dispatch. Created before any subagent is dispatched.

**Schema (required fields — unknown = stop):**

```
run_id, timestamp
task_risk: {level: low|medium|high|critical, reasons: []}
scope: {repo, worktree, branch, dirty_state}
routes: {implementer: {provider, model, quota_signal}, reviewer: {provider, model, quota_signal}, fallbacks: []}
required: [{name, check, status: pass|fail|unknown, failure_action: stop|degraded|ask}]
token_tools: {rtk, codegraph, context-mode, ponytail: active|available|missing}
bypass: {codex_bypass: bool, claude_bypass: bool, invariant_asserted: bool}
verification: {tier: targeted|milestone|full}
decision: dispatch|degraded|stop
```

**Rule:** No dispatch until a valid row exists with `decision: dispatch`. If any mandatory field is unknown, safety/scope checks fail, or required items have no declared failure_action, the decision defaults to `stop` — not optimism. The concrete path (`~/.hermes/...` etc.) is specified by the agent's adapter or global config.

### Two paths

1. **LLM-orchestrator path** (interactive dispatch): the orchestrator writes the rich row (risk, routes, required[], token_tools) from its own reasoning before dispatching subagents. This is a true gate — `decision: stop` blocks dispatch. Enforced by the brief contract (§3) and the orchestrator's own discipline.

2. **Mechanical dispatch path** (autonomous/kanban workers): a pre-spawn helper writes a minimal observational row with `decision: log-only`. The mechanical path cannot compute `required[]`, `token_tools.active`, or `task_risk` — those fields are the LLM orchestrator's responsibility. This path is a **log**, not a gate. The gate for autonomous work lives upstream: pre-spawn guards, profile concurrency caps, and stale-timeout detection.
