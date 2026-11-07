# AI Orchestration Playbook

![type: documentation](https://img.shields.io/badge/type-documentation-blue)
![captain: claude code](https://img.shields.io/badge/captain-claude--code-green)

> **One captain. Many hands. You hold the merge button.**

A short operating model for running batches of software work through `dsh-go` packets, with **Claude Code
as the captain**: it decomposes, briefs, dispatches, verifies and integrates — and never writes the product
code. The agent-facing artifact is [`PLAYBOOK.md`](./PLAYBOOK.md); read it once, follow it for the run.

## The model in one diagram

```text
              ┌──────────────────────────────────────┐
              │            YOU  (Radan)              │
              │  decisions · merge go · deploy go    │
              └────────▲───────────────────▲─────────┘
                       │ asks              │ authorizes each merge
              ┌────────┴───────────────────┴─────────┐
              │   CAPTAIN  (one Claude Code session) │
              │  decompose · dispatch · verify · git │
              └───┬──────────┬──────────┬────────────┘
                  │ brief    │ brief    │ brief
             ┌────▼────┐ ┌───▼─────┐ ┌──▼──────┐
             │ dsh-go  │ │ dsh-go  │ │ dsh-go  │
             │ worktree│ │ worktree│ │ worktree│
             └─────────┘ └─────────┘ └─────────┘
        Implementer: DeepSeek v4.1 Flash · Reviewer: Muse Spark 1.3
```

**In one sentence:** the captain owns the board, packets write the code in isolated worktrees, a different
model reviews every result, and you authorize every merge.

## Why you'd want this

- **Parallelism without chaos.** File-disjoint packets, one worktree each, no collisions and no entangled blame.
- **You stay in control.** The captain pauses for scope-changing decisions, and never merges or deploys
  without your explicit go — per PR.
- **Nothing integrates unreviewed.** Every result is read by a model that did not write it.
- **Dead packets don't block you.** A timed-out agent is inherited by a fresh one, never hand-patched.
- **It improves from use.** Every run that exposes a new failure mode feeds the playbook or the adapter.

## Install

```bash
git clone git@github.com:radandevist/ai-orchestration-playbook.git ~/ai-orchestration-playbook
```

Then add one line to your global Claude Code instructions (`~/.claude/CLAUDE.md`):

```markdown
For multi-task orchestration, follow `~/ai-orchestration-playbook/PLAYBOOK.md`
and the current repository's `.ai/orchestration-adapter.md`.
```

Updating is `git -C ~/ai-orchestration-playbook pull`; the reference line never changes.

## Onboard a project

Copy [`adapter-template.md`](./adapter-template.md) to `<repo>/.ai/orchestration-adapter.md` and fill the
seven fields: `default_branch`, `setup_cmd`, `test_cmd`, `acceptance_cmd`, `worktree_root`, `report_dir`,
`known_quirks`. Add a one-line pointer under an "AI Orchestration" heading in the repo's `AGENTS.md`.

No adapter yet? The captain creates that short file on its first pass and continues — it never stops for a
missing adapter.

## Using it

Tell the captain what you want, at batch scale: *« orchestre les correctifs de ces 7 issues »*, *« découpe
ce service de 3000 lignes et livre chaque extraction en PR »*, *« transcris la réunion et livre »*.

It reads the playbook, picks the skill from the situation table (§9) — meeting, delivery, merge, preprod
deploy, QA, captures, Word proof, volet, video, new feature, bug, second opinion, archives, session notes
— and runs the loop:

| Step | Captain | You |
|---|---|---|
| Decompose, brief, dispatch | splits into file-disjoint packets in worktrees | — |
| Verify | reruns the tests, reads the diff, runs the full gate | — |
| Review | has another model review the exact pushed commit | — |
| Close | the seven-point checklist (`PLAYBOOK.md` §8) | — |
| Merge / deploy | only after your go | **you authorize** |

## The optional `pr-closure` gate

`tools/pr-closure` is a fail-closed CLI that derives one state per PR from Git, GitHub, local verification
and review records. It is **not** needed for the normal loop; the seven-point checklist is enough. Its
state machine and commands are documented in [`appendix-pr-closure.md`](./appendix-pr-closure.md).

## What's in this repo

| File | What it is |
|---|---|
| [`PLAYBOOK.md`](./PLAYBOOK.md) | The operating model. The core artifact. |
| [`appendix-pr-closure.md`](./appendix-pr-closure.md) | Optional `pr-closure` state machine and CLI. |
| [`adapter-template.md`](./adapter-template.md) | Seven-field per-repo adapter. |
| [`captain-packet-template.md`](./captain-packet-template.md) | Board, packet skeleton, `dsh-go` launch shapes. |
| [`tools/pr-closure`](./tools/pr-closure) + `tools/schemas/` | The gate CLI and its schemas. |
| [`CHANGELOG.md`](./CHANGELOG.md) | Version log — what changed and why. |

## Sync model

This repo is the master; consumers update with `git pull` on each host. There is no auto-push, so drift
stays visible in [`CHANGELOG.md`](./CHANGELOG.md).

## License

No license file is published. Until one is added, GitHub's default host-and-view terms apply — reach out
before redistributing.
