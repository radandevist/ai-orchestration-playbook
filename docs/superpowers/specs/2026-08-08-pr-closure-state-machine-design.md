# PR Closure State Machine and Mechanical Gate

> **Historical, non-normative model routing and issue admission:** The Luna/`xhigh` instructions below record the policy used when this design was written (2026-08-08) and are **superseded and non-normative**. Current dispatches must follow the live [PLAYBOOK model policy](../../../PLAYBOOK.md): exactly one cross-family GPT-5.6 Sol review at `high` (the final adversarial judgment before merge), with no reviewer shotgun, fallback cascade, or Claude-family calls. Issue admission follows the live §2.6 ladder (`BLOCKS_PR` → link existing root issue → `FOLLOW_UP_ISSUE` only for a concrete reproducible independent root defect → `NOTE_ONLY`); every admitted deferred root defect has one verified root issue, and disposition is decided by admission, not severity.

**Date:** 2026-08-08
**Scope:** Every project using the AI Orchestration Playbook
**First live adoption:** PublyApp's six open pull requests

## Problem

The playbook requires independent adversarial review, but its current review loop has no
mechanical definition of progress or closure. An agent can repeatedly:

- review a branch whose CI is already red;
- fix the example a reviewer found instead of the rule that produced it;
- lose or misread a verdict stored under a temporary session directory;
- review commits that were never pushed;
- call a milestone review an approval of the whole pull request;
- keep non-blocking observations in the branch instead of filing follow-up issues;
- announce status from memory while GitHub, Trello, and the worktree disagree; or
- treat a failed API call, missing artifact, or unknown result as evidence of a benign state.

Digital Prevention and PublyApp runs show that reminders do not reliably prevent these failures.
The durable solution must derive state from sources of truth and refuse invalid transitions.

## Goals

1. Preserve full, cross-family adversarial review for every pull request.
2. Make pull-request progress and approval machine-checkable.
3. Separate findings that must block the current pull request from valuable follow-up work.
4. Fail closed when evidence is missing, stale, malformed, or contradictory.
5. Prevent the same repair strategy from repeating when the same root cause survives.
6. Keep evidence outside temporary agent-session directories.
7. Keep GitHub, the worktree, Trello, and owner-facing status synchronized from one derived state.
8. Generalize across projects while leaving project-specific commands in each adapter.

## Non-goals

- Limiting the number or depth of adversarial reviews.
- Automatically merging pull requests.
- Allowing low-severity labels to excuse a broken acceptance requirement.
- Making Trello a source of truth.
- Replacing project CI or project-specific acceptance commands.
- Automatically deciding genuine product, security, cost, or scope questions for the owner.

## Core rule

The system never stores or accepts a free-form claim that a pull request is ready. It derives
readiness from evidence tied to the same pushed commit:

1. local worktree is clean;
2. local branch tip equals the remote branch tip;
3. required local verification passed at that tip;
4. GitHub CI passed at that tip;
5. the latest independent review names that exact tip;
6. the review has no blocking findings;
7. every deferred finding has a real follow-up issue — **superseded wording (live §2.6):** every admitted deferred root defect has one verified root issue; and
8. the review verdict is `APPROVED` or `APPROVED_WITH_FOLLOW_UPS`.

Unknown, missing, stale, or contradictory evidence is `UNVERIFIED`, never success.

## State machine

Each pull request has exactly one derived state:

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
| `FOLLOW_UP_FILING` | No blockers remain, but follow-up findings lack issue IDs. **Superseded (live §2.6):** no blockers remain, but an independently admitted root defect lacks an issue ID. | File and verify the issues. |
| `APPROVED_WITH_FOLLOW_UPS` | CI and local gates are green; review approved; every deferred finding has an issue. **Superseded (live §2.6):** every admitted deferred root defect has one verified root issue. | Report ready and wait for owner merge authority. |
| `APPROVED` | CI and local gates are green; review approved with no required follow-ups. | Report ready and wait for owner merge authority. |
| `NEEDS_OWNER` | A genuine owner decision blocks progress. | Ask one narrow decision question. |
| `STALLED` | No qualifying progress occurred within the adapter's time budget, or repeated executors died without producing evidence. | Rescue the lane, redesign the packet, or move to `NEEDS_OWNER`; another identical dispatch is forbidden. |
| `UNVERIFIED` | Required evidence is missing, malformed, stale, or contradictory. | Restore evidence; never infer a more favorable state. |

Approval is invalidated immediately by any new commit, changed CI result, reopened blocker,
missing follow-up issue, or mismatch between reviewed, local, remote, and CI commit IDs.

## Review finding model

Every finding has two independent classifications.

### Severity

- `CRITICAL` — security, authorization, privacy, irreversible data loss/corruption, or a central
  correctness failure with severe impact.
- `MAJOR` — a broken acceptance requirement, regression, central-claim failure, or realistic
  production failure.
- `MEDIUM` — a bounded correctness, robustness, test-strength, operability, or maintainability
  problem with meaningful impact.
- `MINOR` — low-impact hygiene, clarity, or localized maintainability work.
- `NOTE` — useful observation with no requested change.

### Disposition

- `BLOCKS_PR` — must be fixed and independently re-reviewed before approval.
- `FOLLOW_UP_ISSUE` — may leave the branch only after a real issue is filed and linked. **Superseded (live §2.6):** `FOLLOW_UP_ISSUE` is admitted only for a concrete reproducible failure whose root cause is independent of the branch's central claim; reuse the existing root issue when one exists, and never mint several issues for one cause.
- `NOTE_ONLY` — recorded in the review; no issue required.

Severity does not decide disposition by itself. Scope, causality, and risk do. A major pre-existing
weakness can be follow-up work; a medium defect in the pull request's central claim can block.

### Findings that always block

The following cannot be deferred, regardless of the reviewer's chosen label:

- branch-caused red CI;
- an unmet acceptance requirement;
- a regression introduced or exposed by the branch;
- security, authorization, privacy, billing, or data-integrity uncertainty;
- failure or unverifiability of the pull request's central claim;
- a guard that silently treats an undecidable input as safe;
- a test or verification mechanism that cannot detect the defect it claims to detect;
- unpushed work, dirty probe residue, or a reviewed-tip mismatch; and
- the same underlying blocker returning after an attempted fix.

The gate promotes any such finding to `BLOCKS_PR` if a reviewer misclassifies it.

## Review verdicts

Reviewers return exactly one machine-readable verdict:

- `CHANGES_REQUIRED` — one or more blocking findings.
- `APPROVED_WITH_FOLLOW_UPS` — no blockers; all follow-up findings are validly deferred.
- `APPROVED` — no blockers or required follow-ups.
- `INCONCLUSIVE` — the reviewer could not establish the central claim or complete the required
  evidence. This blocks approval but is not automatically a code accusation.

The human-readable report remains adversarial and detailed. A structured JSON companion is
the authority consumed by the gate. The schema includes:

- schema version;
- repository and pull-request number;
- reviewed branch and commit ID;
- base commit or comparison range;
- implementer and reviewer model families;
- local and CI evidence references;
- verdict;
- findings with stable IDs, root-cause class, severity, disposition, scope, evidence, and paired
  bad-case/good-case proof where applicable;
- follow-up issue number for every `FOLLOW_UP_ISSUE`; and
- explicit intentionally-not-a-finding notes.

An unknown schema version, unknown verdict, duplicate finding ID, missing issue ID, or malformed
record produces `UNVERIFIED`.

## Preserving adversarial review

The closure gate reduces wasted cycles, not review pressure.

### Default model lanes

- Implementation defaults to DeepSeek V4 Flash.
- Independent review defaults to GPT-5.6 Luna at `xhigh` reasoning effort.
- Claude models are not used for new implementation, review, or coordination work.
- Historical Claude review artifacts remain valid evidence when they already satisfy the structured
  cross-family contract; they are not rerun solely because the default changed.
- The mechanical rule remains model-family separation: an OpenAI-family reviewer may review a
  DeepSeek implementation, but it may not review an OpenAI-family implementation.

- Every `REVIEW_READY` commit receives a fresh independent cross-family review.
- Any blocking finding returns the pull request to implementation.
- Every fix receives another independent review after CI and local verification are green.
- There is no maximum review count.
- Reviewers must try to falsify the central claim, use mutation or real execution when suitable,
  and provide both the failing escape and the legitimate control.
- A reviewer may surface unlimited non-blocking findings, but each must receive an explicit
  disposition.

## Repeated-root-cause circuit breaker

Reviews continue indefinitely if they keep finding genuinely new blockers. What may not continue is
the same repair strategy for the same root-cause class.

Each blocking finding carries a normalized `root_cause` value. If that root cause survives two
attempted repairs:

1. transition to `DESIGN_RESET`;
2. forbid another syntax-instance patch;
3. require a short mechanism-level explanation of the wrong default or missing invariant;
4. require a structural or mechanical control that prevents bypass;
5. require paired proof and a mutation that disables the new control; and
6. send the redesigned mechanism through another full adversarial review.

This is not a review cap and cannot be used to defer the blocker.

## Stagnation circuit breaker

Activity is not progress. A running process, another review number, another attempted dispatch, or
another green fixture count does not reset the stagnation clock.

Each adapter sets a `stagnation_budget` appropriate to the project. When no qualifying progress
event occurs inside that budget, or two executors die on the same packet without producing valid
evidence, the pull request enters `STALLED`. The captain must then do one of the following:

- rescue from a clean checkpoint with a changed execution strategy;
- reduce or rewrite the packet around the actual blocker;
- enter `DESIGN_RESET`; or
- move to `NEEDS_OWNER` for a genuine external decision.

Repeating the same dispatch is forbidden. `STALLED` never relaxes review, CI, or approval gates.

## Sources of truth and derived projections

Sources of truth, in order:

1. Git worktree and remote refs;
2. GitHub pull-request metadata and CI results;
3. durable local verification records;
4. durable structured review records;
5. GitHub follow-up issues.

Trello, dashboards, chat summaries, and notifications are derived projections. They never override
the sources above. An API failure is an unknown state; an empty result is not substituted.

The gate must refuse mutating synchronization when any required source is unavailable. It may print
the last known state only when clearly marked stale and never as approval.

## Durable run storage

Every run uses a stable directory outside temporary agent-session folders:

```text
<orchestration_home>/runs/<project>/<pr-number>/
  state.json
  events.jsonl
  verification/<commit>.json
  reviews/<commit>/<review-id>.json
  reports/<commit>/<review-id>.md
  briefs/<packet-id>.md
```

`state.json` is a cache derived from events and live sources, not the authority. Evidence under
`~/.claude/jobs`, `/tmp`, or another session-owned directory may be copied into the durable run but
may never be the sole approval evidence.

All mutations append an event. Existing evidence is never silently overwritten.

## Execution harness rules

The reusable harness enforces lessons that prose repeatedly failed to enforce:

- one writing lane per worktree;
- worktree paths are resolved from Git rather than reconstructed from naming conventions;
- briefs are files, never shell-expanded prompt strings;
- model family, provider, harness, and effort are recorded as separate fields;
- concurrency limits belong to the harness or heavy resource, not to orchestration as a whole;
- heavy verification uses a lease so two full suites cannot overlap accidentally;
- exit code zero with missing, tiny, malformed, or unparseable output is failure;
- liveness uses growing output or advancing CPU time, not merely a live process ID;
- orphaned descendants remain owned by the lane until explicitly accounted for;
- a dead lane's partial work is never committed blindly; and
- every packet, result, review, and status event is copied into durable run storage.

## Mechanical gate

The playbook provides one reusable command, provisionally:

```bash
pr-closure status --repo <path> --pr <number>
pr-closure check-transition --repo <path> --pr <number> --to <state>
pr-closure sync --repo <path> --pr <number>
```

The command:

- resolves the live worktree rather than constructing its path;
- checks clean/pushed/reviewed/CI commit equality;
- calls adapter-provided commands for project verification;
- distinguishes code CI failures from proven infrastructure failures;
- validates review records against the schema;
- checks mandatory-block rules independently of reviewer labels;
- verifies deferred issue numbers exist and are open or deliberately scheduled;
- detects stale approvals and repeated root causes;
- prints the derived state and exact missing evidence; and
- synchronizes Trello only after a successful read of every required source.

Exit codes are stable and documented. Missing evidence and tool/API failures are non-zero.

## Adapter contract additions

Every project adapter must add:

- `ci_status_cmd` — read current required checks for the exact pull-request tip;
- `ci_rerun_cmd` — bounded rerun command for proven infrastructure failures, or `none`;
- `local_review_ready_cmd` — the minimum local gate before adversarial review;
- `closure_acceptance_cmd` — the full gate required for approval;
- `closure_state_dir` — durable project run directory;
- `review_schema` — supported review-record schema version;
- `review_publication_cmd` — optional command to publish a compact review link/summary, or `none`;
- `follow_up_issue_cmd` — create and verify a linked follow-up issue;
- `tracking_projection` — Trello/project-board mapping or `none`;
- `infra_retry_budget` — maximum automatic reruns before `NEEDS_OWNER`;
- `stagnation_budget` — maximum time without a qualifying progress event;
- `lane_liveness_cmd` — project/host command for output or CPU-time advancement;
- `lane_output_floor` — minimum valid result size plus structured-result requirement;
- `heavy_job_limit` — mechanical concurrency limit for full builds/e2e/API suites; and
- `central_claim_rules` — project-specific claims that may never be deferred.

Adapters remain declarative. They do not reimplement the state machine.

## Packet and reviewer contract changes

Implementation packets must name:

- the starting derived state and exact commit;
- blocking finding IDs being closed;
- the normalized root cause and prior repair strategies;
- required local and CI gates;
- paired proof requirements;
- expected final transition; and
- any brief statement the implementer found to be wrong.

Review packets must name:

- the exact pushed commit and successful gate records;
- the central claim to falsify;
- all previously blocking finding IDs;
- the mandatory-block rules;
- required structured output path; and
- the rule that unresolved evidence is a loud refusal, not a silent pass or unsupported accusation.

## Progress definition

A round counts as progress only when at least one is true:

- a named blocking finding is closed with new evidence;
- CI changes from branch-caused red to green;
- an infrastructure failure is proven and successfully rerun;
- a repeated root cause enters `DESIGN_RESET` with a different mechanism;
- a non-blocking finding receives a verified follow-up issue; or
- the pull request advances to an approval state.

Starting or completing another review is not progress by itself.

## Review visibility

The structured review record is the approval authority. The gate may also publish a compact review
summary or link on the pull request when the adapter provides `review_publication_cmd`. It must not
pretend that a self-authored GitHub review is an independent approval, and GitHub's empty
`reviewDecision` field does not erase a valid cross-family review record. Owner-facing status must
say where the authoritative review lives.

## PublyApp first-live adoption

The six current pull requests are imported without trusting their existing labels or summaries.
For each one the gate will:

1. resolve the live worktree and remote tip;
2. read current GitHub CI;
3. classify red jobs as branch-caused or infrastructure only from logs;
4. import the latest local review into the structured schema;
5. classify every finding by severity and disposition;
6. file verified follow-up issues for eligible non-blocking findings;
7. derive the current state;
8. update the PublyApp Trello projection; and
9. dispatch only the action allowed by that state.

Expected initial states from the live audit:

- three pull requests are CI green but have blocking review findings;
- two have branch-caused CI work;
- one has an infrastructure failure that prevented browser tests from starting;
- the toast pull request is CI green with an active review whose temporary probes must remain owned
  until the review exits.

No pull request is merged by this adoption.

## Testing strategy

The gate needs fixture-driven tests for every paid failure mode:

- missing, malformed, old, and new review formats;
- GitHub API failure versus a genuine empty result;
- clean versus dirty worktrees;
- pushed versus unpushed commits;
- reviewed-tip, local-tip, remote-tip, and CI-tip mismatches;
- milestone approval versus whole-PR approval;
- new commit invalidating approval;
- branch-caused CI failure versus infrastructure failure;
- follow-up disposition without an issue ID;
- mandatory blocker mislabeled as follow-up;
- repeated root cause triggering `DESIGN_RESET`;
- missing durable evidence after a temporary session directory disappears;
- Trello divergence detection without mutation when sources are incomplete;
- unknown schema fields and verdicts failing closed; and
- one heavy-job lease preventing overlapping expensive verification.

Mutation tests must show that removing each fail-closed clause makes a fixture pass incorrectly and
therefore makes the test suite fail.

## Rollout

1. Add the state machine, review model, and adapter fields to the global playbook.
2. Add the review schema and reusable `pr-closure` tool with tests.
3. Update the adapter template and packet template.
4. Migrate Digital Prevention's existing durable harness/board lessons without taking ownership of
   its current uncommitted files.
5. Update PublyApp's adapter and import its six pull requests.
6. Run the gate in read-only mode and reconcile discrepancies.
7. Enable transition enforcement for PublyApp.
8. Migrate other project adapters when they next run; missing closure fields become a preflight stop.

## Success criteria

- No review starts against a commit with branch-caused red CI or incomplete local evidence.
- No approval survives a new commit or commit mismatch.
- No non-blocking finding disappears without a verified issue or explicit note disposition.
- No mandatory blocker can be downgraded by wording alone.
- No repeated root cause receives a third instance-level repair.
- No missing artifact or failed source lookup produces a favorable state.
- Trello and owner-facing summaries are generated from the same derived state.
- Every approved pull request has green CI, green project acceptance evidence, and a fresh independent
  adversarial verdict tied to the same pushed commit.
