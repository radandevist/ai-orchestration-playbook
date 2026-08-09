# Captain Board + Packet Template

Use this when one captain coordinates several clones/worktrees. Keep the board small; old packets belong in the run artifact directory, not in the hot prompt.

## Board

- **Goal:** `<one sentence>`
- **Captain root:** `<absolute parent/coordination directory>`
- **Stable prefix already distilled:** `<playbook, adapter, repo rules, standing constraints>`
- **Provider lanes:** `<DeepSeek implementation roles>; <GPT-5.6 Luna review roles>; <local roles>`
- **Hot backlog target:** `3-5 ready packets`

| Packet | Lane | Target clone/worktree | Status | Notes |
|---|---|---|---|---|
| `<P1>` | `<deepseek|openai-review|local>` | `<absolute path>` | `queued` | `<short note>` |

## Packet

Save each packet as its own Markdown file under the run artifact directory.

```markdown
# Packet <id>: <short name>

## Header

- **Lane:** <deepseek|openai-review|local>
- **Target clone/worktree:** <absolute path>
- **Effort:** <low|medium|high> (`xhigh` only with ledgered escalation)
- **Start checkpoint:** <branch + commit or explicit WIP note>

## Distilled Context

- <Only stable facts needed for this packet>

## Required Reading

- `<absolute path>:<line or symbol>` - <why>

## Work

- <Concrete task scoped to exact files/symbols>

## Expected Artifact

- <diff, review report, design note, verification report, PR body, etc.>

## Verification

- `<exact command>` - expect <result>

## STOP Conditions

- <condition that means report instead of guessing>

## Return Payload

Implementation packets return all of the following, or the lane is a failure even with exit 0:

- **Exact pushed commit** — the 40-hex SHA of the tip actually pushed and verified
- **Starting derived state** — the closure state the packet began from (playbook §2.6)
- **Blocking finding IDs closed** — every `BLOCKS_PR` finding ID this packet resolves
- **Root-cause history** — the normalized `root_cause` and every prior repair strategy already attempted
- **Paired proof** — the failing escape and the legitimate control for each claimed fix
- **Gates run** — exact commands and results, tied to the pushed commit
- **Expected transition** — the exact closure state this packet expects to reach
- **Structured result marker** — the machine-readable marker written to the configured output path
- **Brief statements found wrong** — any statement in the brief that turned out to be false
- Changed files, remaining risks/blockers, and a suggested next packet if obvious

## Reviewer Return

Independent adversarial reviews return the following. Empty, undersized, or markerless review output
is a failure even with exit 0; unresolved evidence is a loud refusal, not a silent pass or an
unsupported accusation.

- **Exact commit/range reviewed** — the reviewed commit (40-hex) plus base commit or comparison range
- **Model families** — implementer family and reviewer family (must differ, playbook §2.6)
- **Verdict** — exactly one of `CHANGES_REQUIRED`, `APPROVED_WITH_FOLLOW_UPS`, `APPROVED`, `INCONCLUSIVE`; `INCONCLUSIVE` blocks approval without automatically accusing the code
- **Findings** — one entry per finding with: unique ID, root cause, severity, disposition, scope, evidence, paired bad-case/good-case proof, and the follow-up issue number where required
- **Intentionally-not-a-finding notes** — explicit records of checked-and-cleared concerns
- **Structured JSON output** — written to the required path per the review schema (`review_schema` in the adapter)
```

## Launch Shapes

Codex packet into a clone/worktree:

```bash
codex exec --ephemeral --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox \
  -C /absolute/target/clone \
  -m <model> \
  - < /absolute/run/packet.md
```

Local/cheap lane packet:

```bash
cd /absolute/target/clone &&
<exact grep/test/log command from the packet>
```
