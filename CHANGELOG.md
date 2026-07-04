# Changelog

All notable changes to the AI Orchestration Playbook are recorded here. This is a **living document** — it is updated from real use, as sessions expose new failure modes and better patterns. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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
