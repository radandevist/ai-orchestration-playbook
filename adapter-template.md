# Orchestration Adapter

<!--
  HOW TO USE:
  1. Copy this file to <repo>/.ai/orchestration-adapter.md and fill EVERY field below.
  2. Add a one-line pointer under an "AI Orchestration" heading in the repo's AGENTS.md, e.g.:

        ## AI Orchestration
        Orchestrated multi-task work follows `.ai/orchestration-adapter.md` (binds the AI Orchestration Playbook to this repo).

  Field names must match the playbook's §4 contract exactly, including the PR closure fields below
  (playbook §2.6). Leave no field blank; use `none` where a field genuinely does not apply. Missing
  closure fields are a preflight STOP for orchestrated PR work. Adapters remain declarative: they
  name commands and limits; they never reimplement the closure state machine.
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
- **provider_lanes:** `<approved DeepSeek/OpenAI/local lanes and their default roles; no new Claude model calls>`
- **hot_backlog:** `<target ready packet count, usually 3-5, plus durable board/packet path if any>`
- **packet_template:** `<repo-specific template path, or ~/ai-orchestration-playbook/captain-packet-template.md>`
- **push_guard:** `<actual push/merge enforcement path: hook, CI gate, soft gate, or none>`
- **known_quirks:** `<HIGHEST-VALUE FIELD: hard-won host/tooling failure modes + their fixes. Add to this whenever a run exposes a new one.>`
- **additive_merge_files:** `<files whose merge conflicts are resolved additively (keep both sides); anything else → STOP>`
- **dump_dir:** `<where squash bodies + working artifacts are written>`
- **issue_hierarchy:** `<epic structure + the sub-issue linking convention/command>`

## PR closure fields (playbook §2.6 — mandatory for orchestrated PR work)

- **closure_config:** `<absolute path to the machine-readable project-closure-v1.json (schema: tools/schemas/project-closure-v1.json), or none if the repo never opens PRs>`
- **ci_status_cmd:** `<read current required GitHub checks for the exact pull-request tip>`
- **ci_rerun_cmd:** `<bounded rerun command for proven infrastructure failures, or none>`
- **local_review_ready_commands:** `<the minimum local gate(s) before an adversarial review may start — exact closure_config key>`
- **closure_acceptance_commands:** `<the full gate(s) required for approval — exact closure_config key>`
- **closure_state_dir:** `<durable absolute project run directory — never /tmp or a session-owned folder — exact closure_config key>`
- **ci_required_checks:** `<optional exact GitHub check names that are authoritative for CI closure; absent or empty keeps strict all-rollup behavior — exact closure_config key>`
- **review_schema:** `<supported review-record schema version, currently 1>`
- **review_publication_cmd:** `<publish a compact review link/summary on the PR, or none>`
- **follow_up_issue_cmd:** `<create and verify a linked follow-up issue>`
- **tracking_projection:** `<explicit project-board mapping identifier (e.g. trello:publyapp), or none — exact closure_config key; see the safe projection boundary below>`
- **projection_adapter:** `<absolute, non-symlink path to the projection executable — harness binding passed to pr-closure sync as --projection-adapter; required only when tracking_projection is non-none>`
- **infra_retry_budget:** `<maximum automatic reruns of proven infrastructure failures before NEEDS_OWNER — exact closure_config key>`
- **stagnation_budget_minutes:** `<maximum time without a qualifying progress event before STALLED — exact closure_config key>`
- **lane_liveness_cmd:** `<output-growth or CPU-time advancement check for lane liveness>`
- **lane_output_floor:** `<minimum valid result size plus structured-result requirement>`
- **heavy_job_limit:** `<mechanical concurrency limit for full builds / e2e / API suites — exact closure_config key>`
- **central_claim_rules:** `<project-specific claims that may never be deferred>`

The machine-readable `closure_config` carries the mechanically enforced subset with its exact key
names — `schema_version`, `project`, `repository`, `repo_path`, `default_branch`,
`closure_state_dir`, `local_review_ready_commands`, `closure_acceptance_commands`,
`ci_required_checks`,
`infra_retry_budget`, `stagnation_budget_minutes`, `heavy_job_limit`, `tracking_projection` (see
`tools/schemas/project-closure-v1.json`). The fields above are the declarative project bindings the
harness resolves against; the gate itself reads only the `closure_config` JSON.

**Safe tracking-projection boundary.** `tracking_projection` is the explicit board mapping
identifier (for example `trello:publyapp`) or `none`; it is mapping data passed to the projection
executable, never an executable itself. When it is non-`none`, the gate requires the separate
`--projection-adapter /absolute/non-symlink/executable` argument (the adapter doc binds that path in
`projection_adapter`). The gate invokes that executable as an argv list — never through a shell —
over a versioned, bounded JSON protocol (deadline and output caps included), passing the mapping as
`--mapping`. The adapter never emits secrets on stdout. The gate distinguishes dry-run (`sync`
without `--apply`) from apply (`sync --apply`), and the project board is selected explicitly by the
mapping, never guessed. A projection failure never mutates authoritative closure evidence.
