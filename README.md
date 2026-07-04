# ai-orchestration-playbook

A portable, agent-neutral playbook for orchestrating multi-task software work through executors. The agent-facing artifact is **[`PLAYBOOK.md`](./PLAYBOOK.md)** — a single self-contained document covering the non-negotiable discipline, the seven-phase orchestration loop, the executor brief contract, and the per-project adapter contract.

This repo is consumed by AI orchestrating agents (Claude, Hermes, and others). It is documentation only — no code, no scripts.

**The model in one sentence:** one *captain orchestrating agent* owns the board and never touches project code itself — it decomposes work, dispatches bounded *executors* in isolated git worktrees/clones, runs an independent review pass on each result, and integrates — while you (the human) make the judgment calls and authorize every merge.

---

## Contents

- [`PLAYBOOK.md`](./PLAYBOOK.md) — the agent-facing operating model (read top-to-bottom before a run).
- [`adapter-template.md`](./adapter-template.md) — copy into a repo to bind the playbook to that project.
- [`captain-packet-template.md`](./captain-packet-template.md) — board + packet template for one-captain, multi-clone/provider-lane runs.
- [`CHANGELOG.md`](./CHANGELOG.md) — living-doc version log.

---

## Installation (once per agent)

Each agent loads `PLAYBOOK.md` by referencing a **local clone** from its global-instruction file. Clone once per host, then add a single reference line.

### Claude Code

```bash
# 1. Clone to a stable path (any path works; this is the convention used below)
git clone git@github.com:radandevist/ai-orchestration-playbook.git ~/ai-orchestration-playbook
```

Then add this to `~/.claude/CLAUDE.md` (user-global — loads in **every** project):

```markdown
## Orchestration
For multi-task orchestration, follow `~/ai-orchestration-playbook/PLAYBOOK.md`
and the current repository's `.ai/orchestration-adapter.md`.
```

### Hermes (or any other agent)

```bash
# On the agent's host (e.g. the gow machine), clone the repo.
# Private repo → the host needs credentials first (deploy key or PAT) — one-time setup.
git clone git@github.com:radandevist/ai-orchestration-playbook.git ~/ai-orchestration-playbook
```

Then reference `~/ai-orchestration-playbook/PLAYBOOK.md` from that agent's global-instruction slot (the equivalent of `CLAUDE.md`), with the same wording as above.

### Updating

```bash
git -C ~/ai-orchestration-playbook pull
```

The reference line in your agent's global instructions never changes — only the file content does. Run this on each host when the playbook is improved (watch [`CHANGELOG.md`](./CHANGELOG.md)).

---

## Onboard a project (once per repo)

Before an agent can orchestrate work in a repository, that repo needs an **adapter** — the file that supplies the project's concrete commands, branch name, worktree convention, and known failure modes.

```bash
# From inside the target repo:
mkdir -p .ai
curl -fsSL https://raw.githubusercontent.com/radandevist/ai-orchestration-playbook/main/adapter-template.md \
  -o .ai/orchestration-adapter.md
# (or just copy adapter-template.md from your local clone)
```

1. Open `.ai/orchestration-adapter.md` and fill **every** field (see [`PLAYBOOK.md` §4](./PLAYBOOK.md) for what each means).
2. Add a pointer under an **"AI Orchestration"** heading in the repo's `AGENTS.md` so any agent discovers it:

   ```markdown
   ## AI Orchestration
   Orchestrated multi-task work follows `.ai/orchestration-adapter.md`
   (binds the AI Orchestration Playbook to this repo).
   ```

The playbook's generic principles resolve against this adapter, so a project's specifics live with the project — not in the shared playbook.

---

## How to use it (running an orchestrated session)

Once installed and the repo is onboarded, you drive a session through your agent in plain language. For a multi-clone effort, open one captain session from the parent/coordination directory, then let it dispatch bounded packets into the target clones named by the adapter. Here's the shape of a run and where **you** stay in control.

### 1. Kick it off

Tell the agent what you want, at batch scale. Examples:
- *"Orchestrate the fixes for these 7 issues in parallel."*
- *"Split this 3000-line service per the proposal and land each extraction as its own PR."*
- *"Coordinate these DigitalPrevention clones from the parent directory; use one captain and dispatch packets into the right clone."*

The agent reads `PLAYBOOK.md` + the repo's adapter, then runs the **seven-phase loop** (`PLAYBOOK.md` §2):

| Phase | What the agent does | Your involvement |
|---|---|---|
| **Decompose** | Breaks the goal into independent, file-disjoint tasks | — |
| **Decision-gate** | Surfaces genuine judgment calls | **You decide** (rename file vs class? split vs allowlist? strict vs lenient?) |
| **Dispatch** | Launches N executors in isolated worktrees, one brief each | — |
| **Rescue** | If an executor dies, spawns a fresh one to inherit its WIP | — |
| **Review** | Runs an independent (cross-model) review on each result | — |
| **Merge** | Rebase → remove worktree → squash-merge → sync | **You authorize each merge** |
| **Close-out** | Links sub-issues, closes tracking issues | — |

### 2. The gates where the agent stops for you

The agent **pauses and waits** at three points — these are guardrails, not bugs:

- **Decision-gate** — a scope-changing choice with no obvious default. The agent asks; it does not guess.
- **Merge authorization** — the agent never merges on its own. You say "merge X" each time (`PLAYBOOK.md` §1.3).
- **Outward-facing actions** — creating repos, pushing, anything irreversible — confirmed before it runs.

### 3. What you can rely on

- **No surprise writes to your main branch** — all work is on feature branches; you merge (`PLAYBOOK.md` §1.2).
- **Nothing integrates unreviewed** — the review loop is mandatory (`PLAYBOOK.md` §1.4); it catches the plausible-but-wrong result that passes tests.
- **Dead executors don't block you** — a hung run is inherited by a fresh executor, not hand-patched (`PLAYBOOK.md` §2.4).

### 4. Feeding the playbook (it improves from use)

When a run exposes a new failure mode, capture it so it never bites twice:
- A **project-specific** quirk (a build flag, a host gotcha) → add it to that repo's `.ai/orchestration-adapter.md` `known_quirks`.
- A **universal** improvement to the procedure → update `PLAYBOOK.md` here, add a `CHANGELOG.md` line, commit, and `git pull` on each host.

---

## Sync model

- **Master = this repo.** Improvements are committed here, tagged, and noted in [`CHANGELOG.md`](./CHANGELOG.md).
- **Consumers update via `git pull`** on each host.
- **Living document.** The playbook gets better as sessions expose new patterns and failure modes.

### Known limitation

There is no mechanism that auto-pushes updates to all agents/hosts at once. Each host pulls independently (e.g. the main workstation + the gow machine = two pulls). The changelog makes any drift visible.
