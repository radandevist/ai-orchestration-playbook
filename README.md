# AI Orchestration Playbook

![type: documentation](https://img.shields.io/badge/type-documentation-blue)
![scope: agent-neutral](https://img.shields.io/badge/scope-agent--neutral-green)

> **One captain. Many hands. You hold the merge button.**

A portable, agent-neutral operating model for orchestrating **batches** of software work through AI coding agents — Claude Code, Codex CLI, Hermes, or any future agent. It is **documentation only**: no code, no scripts, no runtime. You clone it once and point your agent at it.

The agent-facing artifact is **[`PLAYBOOK.md`](./PLAYBOOK.md)** — a single self-contained document an orchestrating agent loads before a run and follows throughout.

---

## Dependencies

The playbook is documentation-only, but it assumes a working AI coding agent setup. You need at least one agent runtime; the rest are optional but make the playbook significantly more effective.

### Required (at least one)

| Agent | What it does | Install |
|-------|--------------|---------|
| **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** | Anthropic's CLI agent — executors, reviews, dispatch | `npm install -g @anthropic-ai/claude-code` |
| **[Codex CLI](https://github.com/openai/codex)** | OpenAI's CLI agent — executors, reviews, dispatch | `npm install -g @openai/codex` |
| **[Hermes Agent](https://hermes-agent.nousresearch.com)** | Nous Research's agent — captains, executors, dispatch | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` |

### Recommended (token optimization & code intelligence)

The playbook references these tools in §5.4 (Installed tooling). They compress context, query code graphs, and enforce minimal-diff discipline — all reduce token spend during orchestrated runs.

| Tool | What it does | Install / Usage |
|------|--------------|-----------------|
| **[RTK](https://github.com/rtk-ai/rtk)** | Compresses noisy shell output before it enters agent context | `cargo install rtk-cli` · usage: `rtk git ...`, `rtk test ...`, `rtk grep ...` |
| **[CodeGraph](https://github.com/colbymchenry/codegraph)** | Queries a code graph index — skip whole-file reads in large repos | `npx @colbymchenry/codegraph` · usage: `codegraph explore "question"` |
| **[Context-Mode](https://github.com/mksglu/context-mode)** | Routes heavy tool output through a sandbox, returns only the summary | `npm install -g context-mode` · usage: `ctx_batch_execute`, `ctx_execute_file`, `ctx_search` |
| **[Ponytail](https://github.com/DietrichGebert/ponytail)** | 7-rung lazy-dev ladder — enforces minimal diffs before writing code | `npx skills add DietrichGebert/ponytail` · active on all agents. Levels: lite/full/ultra/off |

### Also used by the playbook

| Tool | Role |
|------|------|
| **[gh CLI](https://cli.github.com)** | GitHub operations: PR creation, branch management, issue linking |
| **git worktrees** | Isolated branches for each executor — built into git, no install needed |
| **Agent skills** (e.g. [Superpowers](https://github.com/obra/superpowers)) | TDD, verification-before-completion, brainstorming — loaded per task shape |

---

## The model in one diagram

```
                 ┌──────────────────────────────────────────┐
                 │                YOU  (the human)           │
                 │   judgment calls · merge authorization    │
                 └─────────▲────────────────────▲────────────┘
                           │ decides / asks      │ authorizes each merge
                 ┌─────────┴────────────────────┴────────────┐
                 │       CAPTAIN  (orchestrating agent)       │
                 │  decompose · dispatch · review · integrate │
                 │         never touches project code         │
                 └────┬─────────┬─────────┬─────────┬─────────┘
                      │ brief   │ brief   │ brief   │ brief
                 ┌────▼────┐ ┌──▼─────┐ ┌─▼──────┐ ┌▼─────────┐
                 │Executor │ │Executor│ │Executor│ │ Executor │
                 │worktree │ │worktree│ │worktree│ │ worktree │
                 │   #1    │ │   #2   │ │   #3   │ │    #N    │
                 └─────────┘ └────────┘ └────────┘ └──────────┘
                   isolated git worktrees · one self-contained brief each
```

**The model in one sentence:** one *captain orchestrating agent* owns the board and never touches project code itself — it decomposes work, dispatches bounded *executors* in isolated git worktrees, runs an independent review pass on each result, and integrates — while **you** make the judgment calls and authorize every merge.

---

## Why you'd want this

- **Parallelism without chaos.** The captain splits a goal into file-disjoint tasks and runs N executors concurrently in isolated worktrees. No collisions, no entangled blame.
- **You stay in control.** The captain **pauses** at three gates — scope-changing decisions, every merge, and anything outward-facing/irreversible. It asks; it never guesses and never merges on its own.
- **Nothing integrates unreviewed.** Every result gets an independent review pass — ideally from a *different model family* than produced it — before it touches the default branch. This is what catches the plausible-but-wrong result that passes all tests.
- **Dead executors don't block you.** A hung or crashed executor is inherited by a fresh one with its on-disk WIP, not hand-patched.
- **Works with the agents you already use.** Agent-neutral. Claude Code, Codex CLI, Hermes — same playbook, same adapter contract.
- **It improves from use.** A living document: every session that exposes a new failure mode feeds back into the playbook or the project adapter. The `orchestration/ledger/` directory holds real preflight-ledger entries from actual orchestrated runs.

---

## Install in one prompt

```text
Clone https://github.com/radandevist/ai-orchestration-playbook to ~/ai-orchestration-playbook, detect the active agent (Claude Code, Codex CLI, Hermes, or other), and add this line to that agent's correct global instruction file:

For multi-task orchestration, follow `~/ai-orchestration-playbook/PLAYBOOK.md` and the current repository's `.ai/orchestration-adapter.md`.
```

## Quick start

Three steps, once per host. After that, your agent loads the playbook automatically whenever it detects multi-step work.

```bash
# 1. Clone to a stable path on the agent's host
git clone git@github.com:radandevist/ai-orchestration-playbook.git ~/ai-orchestration-playbook

# 2. Add ONE reference line to your agent's global instructions (see below)

# 3. Onboard each repo you want orchestrated (see "Onboard a project")
```

### 2. Point your agent at the playbook

Add this block to your agent's **global** instruction file so it loads in every project:

```markdown
## Orchestration
For multi-task orchestration, follow `~/ai-orchestration-playbook/PLAYBOOK.md`
and the current repository's `.ai/orchestration-adapter.md`.
```

| Agent | Global instruction file |
|-------|-------------------------|
| **Claude Code** | `~/.claude/CLAUDE.md` |
| **Codex CLI** | `~/.codex/AGENTS.md` (`mkdir -p ~/.codex` first) |
| **Hermes** (or any other agent) | that agent's global-instruction slot — the equivalent of `CLAUDE.md` |

> Private repo? The host needs credentials first (deploy key or PAT) — one-time setup before the clone.

### Updating

```bash
git -C ~/ai-orchestration-playbook pull
```

The reference line in your agent's global instructions never changes — only the file content does. Run this on each host when the playbook improves (watch [`CHANGELOG.md`](./CHANGELOG.md)).

---

## Onboard a project (once per repo)

Before an agent can orchestrate work in a repository, that repo needs an **adapter** — the file that supplies the project's concrete commands, branch name, worktree convention, and known failure modes. The playbook's generic principles resolve against this adapter, so a project's specifics live *with the project*, not in the shared playbook.

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

---

## Running an orchestrated session

Once installed and the repo is onboarded, you drive a session through your agent in plain language. For a multi-clone effort, open one captain session from the parent/coordination directory, then let it dispatch bounded packets into the target clones named by the adapter.

### Kick it off

Tell the agent what you want, at batch scale. Examples:

- *"Orchestrate the fixes for these 7 issues in parallel."*
- *"Split this 3000-line service per the proposal and land each extraction as its own PR."*
- *"Coordinate these clones from the parent directory; use one captain and dispatch packets into the right clone."*

The agent reads `PLAYBOOK.md` + the repo's adapter, then runs the **seven-phase loop** (`PLAYBOOK.md` §2):

| Phase | What the agent does | Your involvement |
|-------|---------------------|------------------|
| **1 · Decompose** | Breaks the goal into independent, file-disjoint tasks | — |
| **2 · Decision-gate** | Surfaces genuine judgment calls | **You decide** (rename file vs class? split vs allowlist? strict vs lenient?) |
| **3 · Dispatch** | Launches N executors in isolated worktrees, one brief each | — |
| **4 · Rescue** | If an executor dies, spawns a fresh one to inherit its WIP | — |
| **5 · Review** | Runs an independent (cross-family) review on each result | — |
| **6 · Merge** | Rebase → remove worktree → squash-merge → sync | **You authorize each merge** |
| **7 · Close-out** | Links sub-issues, closes tracking issues | — |

### The gates where the agent stops for you

The agent **pauses and waits** at three points — these are guardrails, not bugs:

- **Decision-gate** — a scope-changing choice with no obvious default. The agent asks; it does not guess.
- **Merge authorization** — the agent never merges on its own. You say "merge X" each time (`PLAYBOOK.md` §1.3).
- **Outward-facing actions** — creating repos, pushing, anything irreversible — confirmed before it runs.

### What you can rely on

- **No surprise writes to your main branch** — all work is on feature branches; you merge (`PLAYBOOK.md` §1.2).
- **Nothing integrates unreviewed** — the review loop is mandatory (`PLAYBOOK.md` §1.4); it catches the plausible-but-wrong result that passes tests.
- **Dead executors don't block you** — a hung run is inherited by a fresh executor, not hand-patched (`PLAYBOOK.md` §2.4).

### Feed the playbook (it improves from use)

When a run exposes a new failure mode, capture it so it never bites twice:

- A **project-specific** quirk (a build flag, a host gotcha) → add it to that repo's `.ai/orchestration-adapter.md` `known_quirks`.
- A **universal** improvement to the procedure → update `PLAYBOOK.md` here, add a [`CHANGELOG.md`](./CHANGELOG.md) line, commit, and `git pull` on each host.

---

## What's in this repo

| File | What it is |
|------|------------|
| **[`PLAYBOOK.md`](./PLAYBOOK.md)** | The agent-facing operating model. **The core artifact.** Read top-to-bottom before a run. |
| [`adapter-template.md`](./adapter-template.md) | Blank fielded template — copy into a repo's `.ai/` to bind the playbook to that project. |
| [`captain-packet-template.md`](./captain-packet-template.md) | Board + packet template for one-captain, multi-clone / provider-lane runs. |
| [`CHANGELOG.md`](./CHANGELOG.md) | Living-doc version log — what changed and why, per release. |
| `orchestration/ledger/` | Real preflight-ledger entries (JSONL) from actual orchestrated sessions — the playbook is battle-tested, not theoretical. |

---

## Sync model

- **Master = this repo.** Improvements are committed here, tagged, and noted in [`CHANGELOG.md`](./CHANGELOG.md).
- **Consumers update via `git pull`** on each host.
- **Living document.** The playbook gets better as sessions expose new patterns and failure modes.

> **Known limitation.** There is no mechanism that auto-pushes updates to all agents/hosts at once. Each host pulls independently (e.g. the main workstation + a second machine = two pulls). The changelog makes any drift visible.

---

## License

No license file is currently published in this repository. Until one is added, the default terms of GitHub's host-and-view grant apply — reach out before redistributing or adapting the content.
