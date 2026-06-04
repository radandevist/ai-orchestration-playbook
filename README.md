# ai-orchestration-playbook

A portable, agent-neutral playbook for orchestrating multi-task software work through executors. The agent-facing artifact is **[`PLAYBOOK.md`](./PLAYBOOK.md)** — a single self-contained document covering the non-negotiable discipline, the seven-phase orchestration loop, the executor brief contract, and the per-project adapter contract.

This repo is consumed by AI orchestrating agents (Claude, Hermes, and others). It is documentation only — no code, no scripts.

## Install (once per agent)

Each agent loads `PLAYBOOK.md` by referencing a local clone from its global-instruction file.

| Agent | How to install |
|---|---|
| **Claude Code** | Clone to a stable path (e.g. `~/ai-orchestration-playbook`). Add a line to `~/.claude/CLAUDE.md` (user-global — loads in every project): *"For multi-task orchestration, follow `~/ai-orchestration-playbook/PLAYBOOK.md` and the current repo's `.ai/orchestration-adapter.md`."* |
| **Hermes** | Clone the repo on Hermes's host (the gow machine) and reference `PLAYBOOK.md` from Hermes's global-instruction slot. **The host needs credentials** (deploy key or PAT) to clone+pull this private repo — one-time setup. |
| **Future agents** | Same pattern: clone once, add a single reference line to that agent's global instructions. |

## Onboard a project (once per repo)

1. Copy [`adapter-template.md`](./adapter-template.md) to `<repo>/.ai/orchestration-adapter.md` and fill **every** field (build/test/lint commands, default branch, worktree convention, known quirks, etc. — see `PLAYBOOK.md` §4).
2. Add a one-line pointer under an **"AI Orchestration"** heading in the repo's `AGENTS.md`:
   ```markdown
   ## AI Orchestration
   Orchestrated multi-task work follows `.ai/orchestration-adapter.md` (binds the AI Orchestration Playbook to this repo).
   ```

The playbook's generic principles resolve against this adapter, so a project's specific commands and failure modes live with the project, not in the shared playbook.

## Sync model

- **Master = this repo.** Improvements are committed here, tagged, and noted in [`CHANGELOG.md`](./CHANGELOG.md).
- **Consumers update via `git pull`** on each host. The reference line in each agent's global instructions never changes — only the file content does.
- **Living document.** When a session exposes a new failure mode, the fix lands as an updated principle (`PLAYBOOK.md`) or a new `known_quirks` entry in the affected project's adapter, committed with a changelog line. The playbook improves from use.

### Known limitation

There is no mechanism that auto-pushes updates to all agents/hosts at once. Each host pulls independently (for the current setup: the main workstation + the gow machine = two pulls). The changelog makes any drift visible.
