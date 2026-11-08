# AI Orchestration Playbook

> v0.3.3 — 2026-11-07. Written for **Claude Code as the captain**. Read it once before a run, then bind
> it to the repo through `.ai/orchestration-adapter.md` (§5). The optional PR-closure state machine
> lives in [`appendix-pr-closure.md`](./appendix-pr-closure.md).

## 1 — Who does what

| Role | Who | Owns |
|---|---|---|
| **Owner** | Radan | Scope decisions, the merge go (per PR), the deploy go. |
| **Captain** | one Claude Code session per board | Decomposing, briefing, dispatching, verifying, reading diffs, all git work (commit, push, PR, rebase/merge, conflicts), the durable record. |
| **Implementer** | `dsh-go` headless packet — trial (Owner rule, 2026-11-08, half a day): the six approved models code in rotation — qwen3.7-plus, glm-5.3-flash, mimo-v2.6-flash, hy4, muse-spark-1.3-contributor, deepseek-v4.1-flash — and the owner then keeps the effective ones | Writing code, running its tests, leaving the changes **uncommitted**, writing a report. **Any model other than DeepSeek is weak until proven:** its brief MUST follow `~/.claude/skills/agent-brief/brief-strict.md`; give it the smaller, well-bounded task of the three. |
| **Reviewer** | `dsh-go` headless packet — trial (Owner rule, 2026-11-08, half a day): reviews ALSO rotate among the six approved models — qwen3.7-plus, glm-5.3-flash, mimo-v2.6-flash, hy4, muse-spark-1.3-contributor, deepseek-v4.1-flash — never the model that wrote the code; **Dual review (owner rule, 2026-11-08, amended same day):** every review runs TWO independent reviewers in parallel, fresh sessions, neither sees the other: the next TWO models of the review rotation (skipping the model that wrote the code). Goal: compare models on the same diff. Merge only when BOTH report zero CRITICAL/MAJOR/MEDIUM; on disagreement the captain checks the finding at file:line and decides. Log in `essai-modeles.md` what each found and what it missed versus the other.; a verdict counts only with its verdict file and the diff on disk | Reading a diff, returning a verdict with its blocking findings. A **fresh** session with a hostile brief: it gets the diff and the issue, never the implementer's report or reasoning. No verdict counts without a verdict file and the diff on disk. |

A `dsh-go` packet has **no git access**: it writes files and a report. The captain is the only writer of
durable state — commits, PRs, ledger, vault, memory.

## 2 — Non-negotiables

1. **Orchestrate, don't execute.** The captain never edits product code, not even a one-line fix: it
   goes back to a packet. Only vision (reading images) and the Drive/Trello connectors are hands-on.
2. **Never commit or push the default branch.** Every change happens on a feature branch in a worktree.
3. **Merge only on Radan's explicit go, one PR at a time.** A previous go never covers the next PR;
   deploys are the same.
4. **Every result is reviewed by TWO fresh sessions that did not write it** before integration (owner,
   2026-11-08): the next two models of the review rotation, in parallel, hostile brief, no implementer
   report (see §1 Reviewer row). Findings go back until neither reports a CRITICAL/MAJOR/MEDIUM.
5. **Every brief is self-contained.** An agent with zero prior context must act correctly from it alone.
6. **One captain per board.** Speed comes from packets and lanes, not two captains re-reading the same world.
7. **Dispatch only from a clean, named checkpoint**: committed, or explicitly stashed and described, with
   the branch and worktree created before launch.
8. **Subagents never write the vault or the memory.** They report; the captain records.
9. **Keep safe capacity occupied.** While a build, a CI run or a long review is in flight, dispatch
   independent read-only work (prep, audit, impact map) instead of idling. Heavy jobs stay serialized —
   one build/e2e suite at a time; CPU and Docker are the limit, not headcount.
10. **Visual consistency, no concession (owner, 2026-11-08, all products).** The same element kind
    (skeleton, card, pill, toast, empty state) looks the same on every page: no page-local variant, change
    the shared component instead. PublyApp and ShopKartOS share one visual language. Every UI brief and
    every UI review brief carries this line.
11. **A redesign screen is judged on the screen, not on a score.** Measured fidelity ≥ 97 % counts only if
    the measurement compares content (a placeholder where the prototype shows data fails), and the captain
    looks at the side-by-side captures before believing the number (#2339: 96 % measured, screen far off).
12. **Composition + Compound Components everywhere (owner, 2026-11-08, PublyApp and ShopKartOS).** UI is
    composed through `children`; a component with parts is a compound (`Card.Header`, `Card.Body`…,
    shared state through context); never a growing list of boolean/config props. Every UI brief and UI
    review brief carries this line.

## 3 — Launching an executor

Everything durable lives in `~/orchestration-runs/<YYYY-MM-DD-topic>/`, never in the session temp folder
(wiped at close): briefs, prompts, answers, logs, done markers, `ledger.md` (agent reports live in the
worktree per §7 — the sandbox cannot write the run folder).

The captain creates the branch and the worktree **before** launching — the packet cannot:

```bash
git -C <clone> worktree add -b <branch> <worktree> origin/<default_branch>
```

Then launch, with the prompt on stdin **from a file** (never `"$(cat brief)"`, quoting breaks):

```bash
R=~/orchestration-runs/2026-11-07-<topic>; X=P1
printf 'Read %s/%s-brief.md first, then do it.\n' "$R" "$X" > "$R/$X-prompt.txt"

setsid -f bash -c "eval \"\$(fnm env)\"; cd <worktree>; nice -n 10 ionice -c3 \
  timeout 10800 dsh-go --profile headless \
  --patch ~/.dsh/patches/go-deepseek-v4.1-flash.yml - \
  < $R/$X-prompt.txt > $R/$X-answer.md 2> $R/$X-err.log; echo exit=\$? > $R/$X-done"

# wait in the background; never busy-poll in the foreground:
until [ -f "$R/$X-done" ]; do sleep 60; done; cat "$R/$X-done"
```

- Implementer: `go-deepseek-v4.1-flash.yml`, timeout 10800 s (2–4 h for a big task).
- Implementer: the six owner-approved models only (`~/.dsh/cordis.patch.yml`, patches `go-<model>.yml`), in rotation during the 2026-11-08 trial; strict brief for any model other than DeepSeek, ≤ 45 min tasks; record each run in `agent-brief/modeles.md` and the run folder's `essai-modeles.md`; an early stop is a failure, rescued by DeepSeek.
- Reviewer: during the 2026-11-08 trial, the next model in the review rotation (`go-<model>.yml`; DeepSeek's turn uses `go-deepseek-v4.1-flash-high.yml`), never the implementing model, strict brief for any model other than DeepSeek; a fake or empty verdict counts as a failure and the review is redone by DeepSeek-high, fresh session, timeout 2700 s (~45 min). Implementers keep the default patch. `xhigh`/`max` only with an escalation reason written in the ledger. **Dual review (owner rule, 2026-11-08, amended same day):** every review runs TWO independent reviewers in parallel, fresh sessions, neither sees the other: the next TWO models of the review rotation (skipping the model that wrote the code). Goal: compare models on the same diff. Merge only when BOTH report zero CRITICAL/MAJOR/MEDIUM; on disagreement the captain checks the finding at file:line and decides. Log in `essai-modeles.md` what each found and what it missed versus the other.
- `exit=0` plus a report plus a real diff is a result. `exit=124` means **timeout**: no result, rescue
  the packet (§6). A full answer with `exit=0` but no report is still a failure — read the answer, the
  error log and the report; the exit code alone proves nothing.
- **Claude subagents only when Radan opens a Claude window in the current session** (« utilise Claude »,
  « finis le quota »). Sonnet codes, Opus reviews — never the same model on both sides.

## 4 — The captain boundary

The captain may: run tests and verification commands, read diffs, commit, push, open PRs, rebase, merge
and resolve conflicts once Radan authorized it, restore build-generated files, inspect the running app.

The captain may not: write or "quickly fix" product code, tests or config; skip a packet because the
change looks trivial; merge or deploy without the go; let a subagent write the vault. A defect found
during verification is a **new packet**, small and scoped, with the failure quoted in the brief.

## 5 — The repo adapter

`<repo>/.ai/orchestration-adapter.md`, plus a one-line pointer under an "AI Orchestration" heading in the
repo's `AGENTS.md`. The short form is enough:

- `default_branch` — never committed or pushed directly. `setup_cmd` — fresh bootstrap.
- `test_cmd` — the targeted tests. `acceptance_cmd` — the full gate, after a rebase, before merge.
- `worktree_root` — where worktrees live; name them `pr<NUMBER>` once the PR exists.
- `report_dir` — a gitignored folder inside the worktree for agent reports, default `.reports/`.
- `known_quirks` — hard-won failure modes and their fix; the highest-value field.

**Adapter missing:** create that short file on the first pass, continue, and say so in the report — never
stop for it. **Full adapter already there:** don't rewrite it; fix only what went stale (models, launch).

## 6 — From "go" to merged

1. **Decompose** into file-disjoint packets, each reviewable and revertible alone; ask Radan only about
   scope-changing choices with no obvious default.
2. **Brief and dispatch** — run the adapter's `setup_cmd` in the fresh worktree BEFORE launching (packets
   may not install), then dispatch each packet into its own worktree (§3, §7). Keep 3–5 packets ready.
3. **Verify** yourself: rerun the tests, read the diff, run `acceptance_cmd` — "done" is a claim, not
   evidence — then push the branch so review and CI bind to that exact tip.
4. **Review** on the exact pushed commit, by a fresh session. Blocking finding → fix packet → re-review.
   **Merge gate (Owner rule, 2026-11-07, replaces the two-round cap):** no merge while a review still has a
   CRITICAL, MAJOR or MEDIUM finding — keep fixing and re-reviewing. Only MINOR findings may be dismissed
   or sent to a follow-up list. The reviewer grades severity **in the product's real context** (users,
   processes, deploy frequency from the adapter): CRITICAL/MAJOR = breaks a real use or loses data;
   MEDIUM = a real but contained defect; a theoretical scenario is MINOR at most. When a round finds new
   holes in the *mechanism the fix added* (not new instances), stop adding mechanism: simplify, or fix the
   one wrong default behind them (see the 2026-08-08 guard-PRs lesson), never another patch per instance.
   **Severity rubric (Owner rule, 2026-11-07 — reviewers inflate severity; paste it into every review brief):**
   - CRITICAL = data loss or leak, security hole, app broken for everyone.
   - MAJOR = a real user cannot do a normal task, or sees wrong information.
   - MEDIUM = it works, but a real user will hit a visible problem (wrong day shown, a control that does
     nothing in a common case).
   - MINOR = everything else: style, test strength, theoretical cases, a few pixels, docs.
   **No scenario, no severity:** a finding above MINOR states the concrete real-use scenario (who does
   what, what breaks); without one it is MINOR automatically. **The captain re-checks** every MEDIUM-or-above
   finding at its file:line before dispatching a fix; if the scenario does not hold, it is downgraded to
   MINOR and the downgrade is written in the report.
5. **Close** — the seven points of §8, all on the same commit.
6. **Merge**, only with the go: rebase if needed → full acceptance gate → remove the worktree →
   squash-merge (body file to the run folder) → sync the default branch. Run `gh pr merge` from the clone.
7. **Rescue** a dead or timed-out packet with a fresh one inheriting its on-disk work, written into the
   new brief; never finish it by hand. **Minors (Owner rule, 2026-11-08):** a MINOR is dismissed by default, with one line in the review log saying why. Open an issue only when a real user could actually hit it one day. Never open a « follow-ups from review » issue that just collects minors.

## 7 — Brief contract

One Markdown file per packet in the run folder, self-contained. Required blocks:

- **Goal** — one paragraph, with Radan's decision quoted word for word.
- **Where** — repo, branch, absolute worktree path, starting commit.
- **Scope / do not touch** — exact files and symbols, and what must stay untouched.
- **Acceptance** — the exact commands and their expected output: `<test_cmd>`, `<acceptance_cmd>`.
- **Secrets** — never read `.env*`, never print a token.
- **Writes** — no git command, no vault or memory, no GitHub/Trello, no install, no network unless listed.
- **Report** — create `<X>-report.md` inside the worktree in the adapter's gitignored report dir (e.g.
  `<worktree>/.reports/<X>-report.md`) within 10 minutes, updated after each file; the run-folder answer
  file is the fallback; final answer starts with `STATUS DONE` or `STATUS BLOCKED`.
- **Time limit** — realistic (2–4 h for a big task), plus: "do it ONCE; if something fails twice the same
  way, write BLOCKED and stop".
- **Commit and PR** — the captain pre-writes the commit message and PR body, and decides `Refs` vs `Closes`.

## 8 — Closure checklist and ledger

Seven points, all true on the same commit before saying a PR is ready:

1. GitHub CI is green on the pushed commit, if the repo has CI.
2. The adapter's full `acceptance_cmd` passes locally, on that same commit.
3. The review by another model covers that same commit and returns a `STATUS` listing its blocking findings.
4. No blocking finding is left open.
5. A new commit invalidates both the review and the verification.
6. A blocking finding is never deferred to "later".
7. Radan has given his go for this merge.

`pr-closure` is not needed for this checklist; it sits in `tools/` and its state machine is documented in
[`appendix-pr-closure.md`](./appendix-pr-closure.md), to be used only if Radan asks.

The ledger is one `ledger.md` per run folder, one line per dispatch — date, task, implementer, reviewer,
strict brief: `2026-11-07 | P1 playbook rewrite | impl deepseek-v4.1-flash | review muse-spark-1.3 | strict yes`.
`strict` is mandatory: `yes` for every model other than DeepSeek (Owner rule, 2026-11-02: only DeepSeek v4.1
Flash is reliable; the other OpenCode Go models get `brief-strict.md`), `n/a` for DeepSeek. The detailed JSONL
ledger is optional.

## 9 — Situation → skill

Radan describes what he wants; the captain picks the skill. This table comes from the skills actually
installed — check the list again rather than guessing.

| Radan says, or the situation | Skill |
|---|---|
| « transcris la réunion », « compte rendu », « qu'est-ce qu'on doit faire » | `meeting-to-actions` |
| « voici la réunion, on y va » (DigitalPrevention, through to delivery) | `reunion` |
| « livre ça », « livre T900 », « où on en est ? » (one task to delivery) | `livre` |
| « crée les tickets », « mets ça dans le Trello », « ouvre une issue » | `dp-create-tasks` or `to-tickets` |
| « la tâche est finie », « prête à merger ? », « on merge ? » | `dp-merge-pr` |
| « déploie », « mets en préprod », untested cards after a merge | `dp-deploy-preprod` |
| « teste en préprod », « vérifie que c'est livré » | `dp-preprod-qa` |
| needs an account, an OTP or a screen on the running app | `dp-preprod-accounts` |
| « captures », « mets les captures sur la PR », « cadre rouge » | `dp-proof-captures` |
| « le Word pour Alain », « ajoute au Word » | `dp-proof-word` |
| « écris le volet », « le Word du volet 3 », « mets à jour la spec » | `dp-volet-spec` |
| « la vidéo du volet », « démo A-à-Z », « façon Zo » | `dp-volet-video` |
| « fais une vidéo » for a feature demo | `dp-demo-video` |
| a new feature, « on ajoute X », anything creative | `brainstorming` → `writing-plans` → `implement` |
| « ça bug », « diagnostique », something broken or slow | `systematic-debugging` or `diagnosing-bugs` |
| « deuxième avis », « vérifie mon raisonnement », « challenge ça » | `second-opinion` |
| « fouille dans les archives », a fact we should already know | `archives-search` |
| end of session, « note ça », durable context | `obsidian-finalize`; at the start, `obsidian-context` |
| about to claim "done" | `verification-before-completion` |

No skill matches? Work without one — **never stop and never ask which skill to use**.

## 10 — Non-code work

Videos, Word documents and annotated captures go through their skills (§9), which carry the pipeline and
the conventions. Common rule: two hostile reviews by two fresh DeepSeek sessions, and
verification **on the real extracted images** — frames, pages, screenshots — not on the description of
what should be there.

## 11 — Working with Radan

- Answer in plain French, short, no ceremony; honest, bounded estimates ("about 2 h, I'll know more after
  the build"), never optimistic ones.
- **Never ask permission to dispatch a packet.** The go is needed for merges and deploys only.
- Verify before saying "not done": check the file, the branch, the deployment. A claim without evidence is a bug.
- Search the archives and the notes before asking a question he has already answered somewhere.
- He describes intent, not skills — pick the skill yourself (§9).

## 12 — Known pitfalls

1. **An agent overruns its limit with no report.** The `done` file is the truth; `exit=124` means timeout — rescue.
2. **An agent loops.** The brief says do it ONCE; twice the same failure means BLOCKED.
3. **The agent cannot write the run folder.** `dsh-go` is read-only outside the worktree — a missing
   run-folder report means read the worktree report + answer/err files.
4. **Stale dependencies in a fresh worktree.** Without `setup_cmd` a fresh tree goes red for reasons
   unrelated to the change — always run `setup_cmd` before dispatch.
5. **An agent merges or rebases as a patch.** Only the captain touches git.
6. **The build rewrites generated files.** Commit them after the build or restore them; never hand-edit.
7. **`pkill -f` or `pgrep` matches itself.** Anchor the pattern or use the PID / the `done` file.
8. **`gh pr merge` from a worktree merges nothing.** Run it from the clone.
9. **The session temp folder is wiped at close.** Deliverables live in `~/orchestration-runs/…`.
10. **`rm` on an unset variable.** Always `"${VAR:?}"`.

Appendix: [`appendix-pr-closure.md`](./appendix-pr-closure.md) — optional `pr-closure` gate.
