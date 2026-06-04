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
- **build_cmd:** `<command that builds the project>`
- **test_cmd:** `<command(s) to run the relevant tests, incl. filtering syntax if any>`
- **lint_cmd:** `<lint / type-check / format-check command(s)>`
- **client_regen_cmd:** `<API-client / generated-artifact regen command, or `none`>`
- **worktree_root:** `<isolated-worktree path convention, e.g. .worktrees/<short-name>>`
- **executor:** `<which executor to dispatch + default effort (≤ high)>`
- **known_quirks:** `<HIGHEST-VALUE FIELD: hard-won host/tooling failure modes + their fixes. Add to this whenever a run exposes a new one.>`
- **additive_merge_files:** `<files whose merge conflicts are resolved additively (keep both sides); anything else → STOP>`
- **dump_dir:** `<where squash bodies + working artifacts are written>`
- **issue_hierarchy:** `<epic structure + the sub-issue linking convention/command>`
