# Appendix — `pr-closure` gate (optional)

The body of [`PLAYBOOK.md`](./PLAYBOOK.md) closes a PR with the seven-point checklist (§8). This appendix
documents the optional mechanical state machine installed in `tools/pr-closure`. Use it only when Radan
asks for it; nothing in the normal loop requires it.

## What it derives

Pull-request closure is a mechanical state machine, not a prose claim. Every PR has exactly one derived
state, computed from evidence tied to the **same pushed commit**: clean worktree, local tip equal to
remote tip, required local gates green at that tip, GitHub CI green at that tip, a fresh independent
review naming that exact tip with no blocking findings, every deferred finding backed by a verified
issue, and a verdict of `APPROVED` or `APPROVED_WITH_FOLLOW_UPS`. Missing, stale, malformed or
contradictory evidence is `UNVERIFIED` — never success.

| State | Meaning | Allowed next actions |
|---|---|---|
| `CI_RED` | A branch-caused required check is red. | Implement or verify a fix. No approval. |
| `CI_INFRA_RETRY` | A required check failed before testing code, with concrete infrastructure evidence. | Rerun the failed job within the adapter's retry budget. |
| `FIXING` | A bounded implementation packet owns named blocking findings. | Commit, push, verify. |
| `LOCAL_VERIFY` | Work is pushed but the adapter's required local evidence is incomplete or stale. | Run the missing gate, one heavy command at a time. |
| `REVIEW_READY` | CI and local gates are green at the pushed tip. | Dispatch an independent adversarial review. |
| `REVIEWING` | One reviewer owns the exact pushed tip. | Wait for a structured verdict; do not edit the reviewed worktree. |
| `NEEDS_RESOLUTION` | GitHub reports mergeability problems (`mergeable=CONFLICTING`). | Resolve conflicts, re-run the required gates, re-check state. |
| `CHANGES_REQUIRED` | The review contains at least one blocking finding. | Create one fix packet accounting for every blocker. |
| `DESIGN_RESET` | The same root-cause class survived two repair strategies. | Replace the mechanism or proof strategy, verify, re-review. |
| `FOLLOW_UP_FILING` | No blockers remain, but follow-up findings lack issue IDs. | File and verify the issues. |
| `APPROVED_WITH_FOLLOW_UPS` | Gates green; review approved; every deferred finding has a verified issue. | Report ready and wait for the owner's merge authority. |
| `APPROVED` | Gates green; review approved with no follow-up. | Report ready and wait for the owner's merge authority. |
| `NEEDS_OWNER` | A genuine owner decision blocks progress. | Ask one narrow decision question. |
| `STALLED` | No qualifying progress within the time budget, or repeated executor deaths without evidence. | Rescue the lane, redesign the packet, or move to `NEEDS_OWNER`; never repeat the identical dispatch. |
| `UNVERIFIED` | Required evidence is missing, malformed, stale or contradictory. | Restore evidence; never infer a more favorable state. |

Only `APPROVED` and `APPROVED_WITH_FOLLOW_UPS` are terminal, and only a terminal state ends ownership of
the PR, its packets, worktrees and reviewer lanes. `NEEDS_OWNER` and `STALLED` pause or force a rescue
but never end ownership. A state-changing action may run only when the derived state equals the intended
target — no prose claim overrides a denied transition.

## Structured verdicts and findings

A reviewer returns exactly one verdict: `CHANGES_REQUIRED` (at least one blocking finding),
`APPROVED_WITH_FOLLOW_UPS` (no blockers, every follow-up has a verified issue), `APPROVED` (no blockers
or required follow-ups), or `INCONCLUSIVE` (the central claim could not be proven, or the required
evidence could not be completed — this blocks approval without accusing the code). An unknown schema
version, unknown verdict, duplicate finding ID, missing follow-up issue ID or malformed record is
`UNVERIFIED`.

Each finding carries a severity (`CRITICAL`, `MAJOR`, `MEDIUM`, `MINOR`, `NOTE`) and a disposition
(`BLOCKS_PR`, `FOLLOW_UP_ISSUE`, `NOTE_ONLY`). Only `BLOCKS_PR` blocks the PR; severity does not decide
the disposition by itself. A `FOLLOW_UP_ISSUE` may leave the branch only after a real issue is filed,
linked and verified open or deliberately scheduled. `NOTE_ONLY` findings stay recorded and cannot reopen
the loop.

**Mandatory blockers** are promoted to `BLOCKS_PR` whatever label the reviewer used: branch-caused red
CI; an unmet acceptance requirement; a regression introduced or exposed by the branch; security,
authorization, privacy, billing or data-integrity uncertainty; failure or unverifiability of the PR's
central claim; a guard that silently treats an undecidable input as safe; a test or verification
mechanism that cannot detect the defect it claims to detect; unpushed work, dirty probe residue or a
reviewed-tip mismatch; and the same underlying blocker returning after an attempted fix.

**Central-claim rules.** Each project adapter declares `central_claim_rules`: claims that may never be
deferred for that project. The reviewer must try to falsify the PR's central claim and provide both the
failing escape and the legitimate control.

**Exact pushed-commit binding.** All evidence — local verification, CI, review — binds to one exact
pushed commit. Approval is invalidated immediately by any new commit, changed CI result, reopened
blocker, missing follow-up issue, or mismatch between reviewed, local, remote and CI tips.
`INCONCLUSIVE`, unresolved evidence and failed source lookups are loud refusals, never silent passes.

**Repeated-root-cause circuit breaker.** Each blocking finding carries a normalized `root_cause`. If the
same root cause survives two attempted repairs, the PR enters `DESIGN_RESET`: no further
syntax-instance patch; require a mechanism-level explanation of the wrong default or missing invariant, a
structural or mechanical control that prevents the bypass, paired proof with a mutation that disables the
control, and a fresh full adversarial review. This is not a review cap and cannot defer the blocker.

**Stagnation and retry escalation.** Activity is not progress: a running process, another review number
or another dispatch does not reset the stagnation clock. No qualifying progress within the adapter's
`stagnation_budget_minutes`, or two executors dying on the same packet without valid evidence, enters
`STALLED`. Proven infrastructure failures may be rerun only within `infra_retry_budget`; exhausting it
enters `NEEDS_OWNER`. Heavy verification (full builds, e2e, API suites) runs under an exclusive heavy-job
lease so two expensive suites never overlap.

## Sources of truth and projections

Compiled authority: versioned immutable model/launcher registries and registered repository policy
floors. Evidence: Git worktree and remote refs; GitHub PR metadata and CI; durable local verification
records; durable structured review records; GitHub follow-up issues. Trello, dashboards, chat summaries
and notifications are derived projections and are never approval evidence. Project adoption events and
external event anchors provide audit, append-integrity and crash-recovery evidence only.

**CI binding modes.** Either `ci_required_checks_source` and `ci_live_pr_workflow` both carry their
canonical values (source-bound: CI policy is re-read from `.ai/project-closure-v1.json` at the PR
candidate tip, which must itself stay source-bound), or both are `null` with empty
`ci_required_checks` and `ci_live_pr_checks` (generic local mode for unregistered projects whose
closure config is local-only: no candidate-tip read, and every check in the PR rollup must pass — an
empty, pending, skipped, or failed rollup is never green). Generic mode is rejected for any repository
with a registered policy floor (for example `PublyApp/publyapp`).

Trello is nevertheless the owner's delivery view: every active or newly created delivery card must carry
`Objectif`, `État actuel`, `Périmètre / ce qui change`, `Liens` (issue/PR when available) and
`Comment tester` with observable steps. A lacunary card is an incomplete projection, never a complete
delivery update. Projection writes are refused when required sources are unavailable, malformed or
contradictory, or when the projection contract is not satisfied; a projection failure never mutates
authoritative closure evidence.

**Safe tracking-projection boundary.** `tracking_projection` is mapping data passed to the projection
executable, never an executable itself. When it is non-`none`, the gate requires the separate
`--projection-adapter /absolute/non-symlink/executable` argument and invokes that executable as an
argv list — never through a shell — over a versioned, bounded JSON protocol (deadline and output caps
included), passing the mapping as `--mapping`. The adapter never emits secrets on stdout. The board is
selected explicitly by the mapping, never guessed; `delivery_cards_complete: true` proves projection
completeness only, never approval.

## The mechanical gate

```bash
pr-closure status --config <project-closure.json> --pr <N>
pr-closure check-transition --config <project-closure.json> --pr <N> --to <STATE>
pr-closure record-review-dispatch --config <project-closure.json> --pr <N> --lane-id <ID>
pr-closure record-repair-strategy --config <project-closure.json> --pr <N> \
  --root-cause <ROOT> --strategy <STRATEGY> --lane-id <ID>
# tracking projection — dry-run (default) and apply forms:
pr-closure sync --config <project-closure.json> --pr <N> \
  --projection-adapter /absolute/non-symlink/executable
pr-closure sync --config <project-closure.json> --pr <N> \
  --projection-adapter /absolute/non-symlink/executable --apply
```

`sync` reads the board mapping from the config's `tracking_projection` key. When it is `none`, no adapter
is needed: `sync` prints the derived state and refuses `--apply`. When it names a mapping (for example
`trello:publyapp`), `--projection-adapter /absolute/non-symlink/executable` is required for both the
dry-run and the apply form. For a `trello:*` mapping the adapter must verify the resulting card
descriptions against the delivery-card contract above and return `delivery_cards_complete: true`; a
missing or false attestation fails `sync` closed in both modes.

`check-transition` is a **mandatory precondition** before every state-changing closure action (dispatch,
fix, rerun, review, follow-up filing, projection apply, ready report). A denied transition stops the
action. Missing evidence and tool/API failures are non-zero exits — fail closed, never infer a favorable
state. Exit codes are stable: `0` read/check succeeded, `2` invalid input, `3` source unavailable or
malformed, `4` transition denied, `5` verification/projection command failed, `6` heavy-job lease
unavailable. Evidence lives in a durable run directory outside temporary session folders; the run's
`state.json` is a cache, never the authority. Empty, undersized or markerless lane output is failure even
with exit 0.

`pr-closure record-verification` runs the configured full acceptance command and is the **single
canonical full gate**: do not run that same full command manually immediately before it. Run focused
preflight checks, then invoke `record-verification` directly; duplicating its acceptance command adds
latency without adding independent evidence.

## Review pressure

The gate reduces wasted cycles, not review pressure: every `REVIEW_READY` commit gets a fresh independent
review pass, every fix is re-reviewed once CI and local gates are green, and there is no maximum review
count.

## Adapter closure fields

A project that uses the gate adds these fields to its adapter, on top of the seven short-form fields:
`closure_config` (absolute path to `project-closure-v1.json`), `ci_status_cmd`, `ci_rerun_cmd`,
`local_review_ready_commands`, `closure_acceptance_commands`, `verification_command_timeout_seconds`,
`closure_state_dir`, `ci_required_checks`, `ci_live_pr_checks`, `ci_required_checks_source`,
`ci_live_pr_workflow`, `review_schema`, `review_publication_cmd`,
`follow_up_issue_cmd`, `tracking_projection`, `projection_adapter`, `infra_retry_budget`,
`stagnation_budget_minutes`, `lane_liveness_cmd`, `lane_output_floor`, `heavy_job_limit`,
`central_claim_rules`. An adapter that lacks them is a preflight STOP for orchestrated PR work — but a
project that never opens PRs answers `none` and needs none of them.

## Why this exists

Reminders did not stop the paid failure modes: reviewing red CI, reviewing unpushed commits, losing
verdicts under temporary session directories, milestone-as-approval, and status from memory while GitHub,
Trello and the worktree disagree. Deriving state from sources of truth and refusing invalid transitions
makes progress and approval machine-checkable.
