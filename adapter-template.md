# Orchestration Adapter

<!--
  HOW TO USE:
  1. Copy this file to <repo>/.ai/orchestration-adapter.md and fill EVERY field below.
  2. Add a one-line pointer under an "AI Orchestration" heading in the repo's AGENTS.md, e.g.:

        ## AI Orchestration
        Orchestrated multi-task work follows `.ai/orchestration-adapter.md` (binds the AI Orchestration Playbook to this repo).

  Field names must match the playbook's §4 contract exactly. Leave no field blank;
  use `none` where a field genuinely does not apply.
-->

- **default_branch:** `<protected main/integration branch — never pushed/committed directly>`
- **setup_cmd:** `<fresh-worktree bootstrap command(s): restore/install/sync>`
- **build_cmd:** `<command that builds the project>`
- **test_cmd:** `<command(s) to run the relevant tests, incl. filtering syntax if any>`
- **lint_cmd:** `<lint / type-check / format-check command(s)>`
- **acceptance_cmd:** `<full post-rebase / pre-merge gate>`
- **client_regen_cmd:** `<API-client / generated-artifact regen command, or none>`
- **worktree_root:** `<isolated-worktree path convention, e.g. .worktrees/<short-name>>`
- **captain_root:** `<absolute parent/coordination directory for one captain, or repo root>`
- **clone_roots:** `<sibling clone/worktree roots the captain may dispatch into, or none>`
- **host_parallelism:** `<safe heavy-resource concurrency rule for this repo/host>`
- **executor:** `<which executor to dispatch + default effort (≤ high)>`
- **model_ladder:** `<fallback order when the primary model/provider rate-limits or hits quota>`
- **provider_lanes:** `<approved Claude/Codex/local lanes and their default roles>`
- **hot_backlog:** `<target ready packet count, usually 3-5, plus durable board/packet path if any>`
- **packet_template:** `<repo-specific template path, or ~/ai-orchestration-playbook/captain-packet-template.md>`
- **push_guard:** `<actual push/merge enforcement path: hook, CI gate, soft gate, or none>`
- **known_quirks:** `<HIGHEST-VALUE FIELD: hard-won host/tooling failure modes + their fixes. Add to this whenever a run exposes a new one.>`
- **additive_merge_files:** `<files whose merge conflicts are resolved additively (keep both sides); anything else → STOP>`
- **dump_dir:** `<where squash bodies + working artifacts are written>`
- **issue_hierarchy:** `<epic structure + the sub-issue linking convention/command>`
