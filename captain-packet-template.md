# Captain Board + Packet Template

One captain, a run folder, a few packets in flight. Save packets in the run folder
(`~/orchestration-runs/<YYYY-MM-DD-topic>/`), never in the session temp directory. Agent reports live in
the packet's worktree (`.reports/`), not in the run folder — the sandbox cannot write outside the worktree.

## Board

- **Goal:** `<one sentence>`
- **Captain:** one Claude Code session; **run folder:** `<absolute path>`
- **Lanes:** implementer DeepSeek v4.1 Flash; reviewer Muse Spark 1.3; local lane for prep and checks
- **Packets ready:** 3–5

| Packet | Model patch | Worktree | Status | Report |
|---|---|---|---|---|
| `P1` | `go-deepseek-v4.1-flash.yml` | `<absolute path>` | `queued` | `<worktree>/.reports/P1-report.md` |

## Packet

```markdown
# Packet P1 — <short name>

## Goal
<one paragraph; Radan's decision quoted word for word>

## Where
- Repo / clone: <absolute path>
- Branch: <branch> — Worktree: <absolute path>
- Start checkpoint: <commit>

## Scope
- Change: <exact files and symbols>
- Do not touch: <files, folders, generated output>

## Acceptance
- `<test_cmd>` — expect <result>
- `<acceptance_cmd>` — expect <result>

(No setup command here: the captain runs the adapter's `setup_cmd` before launch; the packet may not
install.)

## Secrets
Never read `.env*`, never print a token.

## Writes
No git command. No vault, no memory, no GitHub/Trello, no install, no network unless listed here: <none>.

## Report
Create `<worktree>/.reports/P1-report.md` within 10 minutes; update it after each file.
Final answer: first line `STATUS DONE` or `STATUS BLOCKED`, then files changed,
verification output, concerns.

## Time limit
<2–4 h>. Do the work ONCE. If something fails twice the same way, write BLOCKED and stop.

## Commit and PR
<pre-written commit message and PR body; Refs or Closes decided here>

## STOP
- <condition that means report instead of guessing>
```

## Launch

The captain creates the branch and worktree first, runs the adapter's `setup_cmd` there (the packet may
not install), then dispatches; the packet has no git access. Prompt on stdin from a file:

```bash
R=~/orchestration-runs/2026-11-07-<topic>; X=P1
printf 'Read %s/%s-brief.md first, then do it.\n' "$R" "$X" > "$R/$X-prompt.txt"

setsid -f bash -c "eval \"\$(fnm env)\"; cd <worktree>; nice -n 10 ionice -c3 \
  timeout 10800 dsh-go --profile headless \
  --patch ~/.dsh/patches/go-deepseek-v4.1-flash.yml - \
  < $R/$X-prompt.txt > $R/$X-answer.md 2> $R/$X-err.log; echo exit=\$? > $R/$X-done"

until [ -f "$R/$X-done" ]; do sleep 60; done; cat "$R/$X-done"
```

Review packet: same command with `--patch ~/.dsh/patches/go-muse-spark-1.3-contributor.yml` and
`timeout 2700`. Local lane: run the command from the packet directly in the clone.

`exit=0` plus a report plus a real diff is a result; `exit=124` is a timeout to rescue.

## Return payload (review packet)

- Exact commit reviewed (40-hex) and the base it was compared to.
- Model families on both sides (they must differ).
- Verdict: `CHANGES_REQUIRED`, `APPROVED_WITH_FOLLOW_UPS`, `APPROVED` or `INCONCLUSIVE`.
- One entry per finding: ID, root cause, severity, disposition, evidence, bad-case/good-case proof.
- Explicit "checked and cleared" notes, so the captain knows what was covered.
