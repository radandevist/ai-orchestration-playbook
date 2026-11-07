# Changelog

All notable changes to the AI Orchestration Playbook are recorded here. This is a **living document** — it is updated from real use, as sessions expose new failure modes and better patterns. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.3.2] - 2026-11-07 — merge gate by severity

- Replaced the 0.3.1 two-round cap (Radan changed his mind the same day): no merge while a review has a CRITICAL, MAJOR or MEDIUM finding; only MINOR findings may be dismissed or go to a follow-up list. Severity still graded in the product's real context; the "simplify instead of patching new mechanism" lesson is kept.

## [0.3.1] - 2026-11-07 — review stop rule

- Run loop step 4: **at most two review rounds** (first review + one re-review of the fixes), a third only for a remaining BLOCKER; then merge on Radan's go and send MINOR/NOTE findings to a follow-up list. Severity is graded in the product's real context (MAJOR = breaks a real use or loses data). New holes in a mechanism the fix added mean simplify or fix the one wrong default, not another round. Origin: ShopKartOS 2026-11-07, three hostile rounds with falling severity on a deploy script that kept growing (same pattern as PublyApp PR #806 and the 2026-08-08 guard PRs).

## [0.3.0] - 2026-11-07 — Claude Code captain, `dsh-go` packets

- Rewrote `PLAYBOOK.md` for **Claude Code as the default captain** (363 → 208 lines, English): who does what, nine non-negotiables, the captain boundary, and a run loop from "go" to merged.
- Replaced the old launch material with the exact `dsh-go` command: durable run folder under `~/orchestration-runs/<date-topic>/`, prompt on stdin from a file, `setsid` + `timeout`, the background wait loop on the `done` marker, and `exit=124` meaning timeout. Code/review pair: DeepSeek v4.1 Flash codes, Muse Spark 1.3 reviews (1.2 as fallback). The Claude window (Sonnet codes, Opus reviews) opens only when Radan says so in the current session.
- Stated that the captain creates the branch and worktree before launch (packets have no git access) and that the captain never edits product code: a defect found in verification is a new packet.
- Aligned the brief contract: goal with Radan's decision quoted, scope, exact acceptance commands, Secrets block, Writes block, report file inside the worktree's gitignored report dir within 10 minutes, `STATUS DONE`/`STATUS BLOCKED`, realistic time limit, "do it ONCE else BLOCKED". The captain runs `setup_cmd` in the fresh worktree before dispatch, since packets may not install.
- Moved the §2.6 PR-closure state machine to the optional [`appendix-pr-closure.md`](./appendix-pr-closure.md); the body now carries a **seven-point closure checklist** (CI on the pushed commit, full acceptance gate on that commit, review by another model on that commit, no open blocker, a new commit invalidates review and verification, no deferred blocker, Radan's go).
- Simplified the ledger to one `ledger.md` per run folder, one line per dispatch (date, task, implementer, reviewer); the detailed JSONL ledger is optional.
- Added the **situation → skill** table built from the skills actually installed, plus the "no matching skill → work without one, never stop" rule; added the non-code-work rule (two hostile reviews by two different models, verify on the real extracted images) and a "working with Radan" section.
- Added the known pitfalls: silent timeout, agent loops, agent merge/rebase as a patch, build-rewritten generated files, `pkill`/`pgrep` matching itself, `gh pr merge` from a worktree, session temp folder wiped at close, `"${VAR:?}"`; agent cannot write the run folder (worktree `.reports/`); fresh worktree without `setup_cmd` goes red.
- Shortened `adapter-template.md` to seven fields (`default_branch`, `setup_cmd`, `test_cmd`, `acceptance_cmd`, `worktree_root`, `report_dir`, `known_quirks`); a missing adapter is created by the captain on first pass, and existing full adapters are patched (stale model/launch lines), not rewritten.
- Rewrote `captain-packet-template.md` around the `dsh-go` launch shape and the new brief contract; shortened `README.md` in the same spirit.
- Kept Radan's earlier additions: "keep safe capacity occupied" in the body, and the single canonical `record-verification` gate in the appendix.
- Removed the obsolete agent-specific material: old CLI launch commands, Hermes paths and the kanban ledger path, the Luna/Sol model-policy exception, and the Caveman/RTK measurements.

## [0.2.0] - 2026-08-09 — Mechanical PR closure gate

- Added §2.6 PR closure state machine as a mandatory gate: one derived state per PR, exact terminal states (`APPROVED`, `APPROVED_WITH_FOLLOW_UPS`), stale-approval invalidation, design reset, stagnation and infrastructure-retry escalation, and `pr-closure check-transition` as a mandatory precondition before every state-changing closure action.
- Added structured multi-floor reviews: two-axis findings (severity × disposition), exact machine verdicts (`CHANGES_REQUIRED`, `APPROVED_WITH_FOLLOW_UPS`, `APPROVED`, `INCONCLUSIVE`), mandatory-block promotion, and durable structured review records as the approval authority.
- Added a safe tracking-projection protocol: absolute non-symlink adapter invoked as an argv list (never through a shell) over a versioned bounded JSON protocol, dry-run/apply distinction, and projection failure that never mutates authoritative closure evidence.
- Added the Trello delivery-card contract: `pr-closure sync` requires the exact boolean attestation `delivery_cards_complete: true` to confirm each projected card includes Objectif, État actuel, Périmètre/Ce qui change, Liens, and Comment tester; Trello remains a projection, never approval evidence.
- Hardened the fail-closed direction: missing, stale, malformed, or contradictory evidence is `UNVERIFIED`; empty, undersized, or markerless lane output is failure even with exit 0.
- Documented the current all-project model policy: implementation default DeepSeek V4 Flash, independent review default GPT-5.6 Luna at `xhigh`, no new Claude implementation/review/coordination calls, historical Claude artifacts remain usable without rerun, reviewer family must differ from implementer family.
- Corrected the active dispatch and adapter templates: implementation/review launch shapes now select DeepSeek V4 Flash and GPT-5.6 Luna explicitly (no unspecified `<model>` shape), `tracking_projection` is documented as the board mapping with `--projection-adapter` as the separate absolute non-symlink executable, and the `sync` copy/paste examples show both dry-run and `--apply` forms.
- Added the `tools/pr-closure` CLI and schemas to the README with installation, the status command, one-time legacy-verdict import (never silently approved), and project migration/preflight behavior.

## [0.1.3] - 2026-07-04 — Parallelism gates on heavy jobs, not agent headcount

- Clarified §2.3 dispatch STOP-triggers: concurrency is bounded by concurrent **heavy-resource** jobs (Docker/e2e stacks, full builds/test suites), **not** subagent headcount — lightweight agents (investigations, reviews, design, non-Docker impl, unit/typecheck/lint) run many-in-parallel. Serialize only the heavy jobs.
- Host-specific ceilings stay in the repo adapter's `host_parallelism` field (per §4/§5.5), keeping the portable playbook lean rather than encoding one box's core/RAM counts.

## [0.1.2] - 2026-07-04 — Captain lanes for parallel quota speed

- Added the **one captain per work board** rule: multi-clone work should use one hot orchestrator that owns queue/context/routing, while executors run as bounded one-shot packets into target clones/worktrees.
- Added provider-lane dispatch guidance: Claude/Codex/local lanes, hot backlog of 3-5 ready packets, and burst heavy concurrency only for independently briefable packets.
- Added `captain-packet-template.md` with board/packet fields and bounded launch shapes for `codex exec -C <clone>` and `claude -p`.
- Expanded adapter fields with `captain_root`, `clone_roots`, `provider_lanes`, `hot_backlog`, `packet_template`, plus missing existing §4 fields.
- Expanded the preflight ledger with captain and context-budget fields so duplicate fresh input is visible per packet.

## [0.1.1] - 2026-06-04 — README install + how-to-use guide

- Expanded `README.md` with concrete installation commands (per-agent `git clone` + the exact global-instruction reference line), a project-onboarding recipe (adapter download + AGENTS.md pointer), and a **"How to use it"** guide: the seven-phase run shape, the three gates where the agent pauses for the human (decision-gate, merge authorization, outward-facing actions), what the human can rely on, and how to feed improvements back into the playbook/adapters.

## [0.1.0] - 2026-06-04 — Initial playbook

Initial extraction of the orchestration working-style, distilled from the 2026-05-30/31 PublyApp session.

- **`PLAYBOOK.md`** — the agent-facing artifact:
  - §1 Discipline: six non-negotiable guardrails (orchestrate-don't-execute, never push/commit the default branch, never merge without per-request authorization, mandatory review loop, effort ceiling = high / never xhigh, persist + link).
  - §2 The seven-phase loop: decompose → decision-gate → dispatch → rescue → review → merge dance → close-out, each with principle, why, a tagged worked example, and STOP-and-report triggers.
  - §3 Executor brief contract: the portable self-contained dispatch-brief skeleton + a filled example.
  - §4 Adapter contract: the eleven required per-project fields + dual discoverability rule + a worked PublyApp/.NET adapter.
- **`adapter-template.md`** — blank fielded template for onboarding a new repo.
- **`README.md`** — per-agent install, per-project onboarding, sync model.
