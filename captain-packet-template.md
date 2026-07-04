# Captain Board + Packet Template

Use this when one captain coordinates several clones/worktrees. Keep the board small; old packets belong in the run artifact directory, not in the hot prompt.

## Board

- **Goal:** `<one sentence>`
- **Captain root:** `<absolute parent/coordination directory>`
- **Stable prefix already distilled:** `<playbook, adapter, repo rules, standing constraints>`
- **Provider lanes:** `<codex roles>; <claude roles>; <local roles>`
- **Hot backlog target:** `3-5 ready packets`

| Packet | Lane | Target clone/worktree | Status | Notes |
|---|---|---|---|---|
| `<P1>` | `<codex|claude|local>` | `<absolute path>` | `queued` | `<short note>` |

## Packet

Save each packet as its own Markdown file under the run artifact directory.

```markdown
# Packet <id>: <short name>

## Header

- **Lane:** <codex|claude|local>
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

- Changed files or findings
- Commands run + result
- Remaining risks/blockers
- Suggested next packet, if obvious
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

Claude packet into a clone/worktree:

```bash
cd /absolute/target/clone &&
claude -p --model <model> --effort <low|medium|high> \
  --dangerously-skip-permissions \
  "$(cat /absolute/run/packet.md)"
```

Local/cheap lane packet:

```bash
cd /absolute/target/clone &&
<exact grep/test/log command from the packet>
```
