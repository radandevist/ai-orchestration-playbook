# Orchestration Adapter

<!--
  Copy to <repo>/.ai/orchestration-adapter.md and fill every field.
  Add a one-line pointer to it under an "AI Orchestration" heading in the repo's AGENTS.md:

      ## AI Orchestration
      Orchestrated work follows `.ai/orchestration-adapter.md`.

  Short form on purpose: seven fields. Add more only when a run proves it needs them.
-->

- **default_branch:** `<protected branch — never committed or pushed directly>`
- **setup_cmd:** `<fresh-worktree bootstrap: restore / install / sync>`
- **test_cmd:** `<targeted tests, with filtering syntax>`
- **acceptance_cmd:** `<full gate: run after a rebase, before merge>`
- **worktree_root:** `<where worktrees live, e.g. .worktrees/pr<NUMBER>>`
- **report_dir:** `<gitignored directory inside the worktree where agent reports are written, e.g. .reports/>`
- **known_quirks:** `<hard-won failure modes and their fix — the highest-value field; grow it after every run>`

The captain makes sure `report_dir` is gitignored — a report must never reach a commit or a PR.

## Discovery

An agent reads `<repo>/.ai/orchestration-adapter.md` at the start of every run. The one-line pointer in
`AGENTS.md` is what makes it findable by an agent that does not know the `.ai/` convention.

## Missing or stale

- **Missing adapter:** the captain creates this short file on the first pass, continues, and says so in
  the report. Never stop for it.
- **Full adapter already in place:** do not rewrite it. Fix only what went stale — model names, launch
  lines — and leave the rest.

## Using `pr-closure`

Optional, and only at Radan's request. A project that uses it adds the closure fields documented in
[`appendix-pr-closure.md`](./appendix-pr-closure.md): `closure_config`, `ci_status_cmd`,
`local_review_ready_commands`, `closure_acceptance_commands`, `closure_state_dir`, `review_schema`, and
the rest. A project that never opens PRs answers `none` and needs nothing more.
