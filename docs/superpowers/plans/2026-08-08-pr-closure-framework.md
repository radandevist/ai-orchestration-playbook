# PR Closure Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable, fail-closed pull-request closure state machine that ties CI, local verification, adversarial review, follow-up issues, and tracking projections to one pushed commit.

**Architecture:** A small Python standard-library package owns typed records, validation, state derivation, durable append-only evidence, live Git/GitHub reads, and a CLI. Project-specific commands and limits come from a versioned JSON configuration referenced by each orchestration adapter. Markdown reports remain readable by people; versioned JSON records are the only machine authority.

**Tech Stack:** Python 3 standard library, `unittest`, Git, GitHub CLI, JSON, Bash, Markdown.

**Existing-work warning:** The playbook checkout already contains untracked, user-owned
`tools/board.py`, `tools/run-lane.sh`, and `tools/run-review.sh`. They are reference material and paid
failure evidence, not scratch space. Implement in an isolated worktree where they are absent; never
copy over, stage, rewrite, or delete those files. The new tracked paths below do not reuse their
filenames.

---

## File map

- Create `tools/pr_closure/__init__.py` — public package version.
- Create `tools/pr_closure/model.py` — enums and immutable records.
- Create `tools/pr_closure/review.py` — structured review validation and mandatory-block promotion.
- Create `tools/pr_closure/state.py` — pure state derivation.
- Create `tools/pr_closure/store.py` — append-only durable evidence store.
- Create `tools/pr_closure/sources.py` — fail-closed Git and GitHub readers.
- Create `tools/pr_closure/lease.py` — exclusive heavy-job lease.
- Create `tools/pr_closure/cli.py` — command-line orchestration boundary.
- Create `tools/pr-closure` — executable wrapper.
- Create `tools/schemas/review-record-v1.json` — published review contract.
- Create `tools/schemas/project-closure-v1.json` — published project-config contract.
- Create `tools/tests/fixtures/` — paid-failure fixtures.
- Create `tools/tests/test_review.py` — review parsing and disposition tests.
- Create `tools/tests/test_state.py` — state-transition tests.
- Create `tools/tests/test_store.py` — durable evidence and stale-approval tests.
- Create `tools/tests/test_sources.py` — live-source failure semantics.
- Create `tools/tests/test_lease.py` — heavy-job exclusion tests.
- Create `tools/tests/test_cli.py` — end-to-end CLI fixtures.
- Modify `PLAYBOOK.md` — normative closure rules.
- Modify `adapter-template.md` — required closure configuration fields.
- Modify `captain-packet-template.md` — finding IDs, root causes, and structured outputs.
- Modify `README.md` — installation and first-use commands.
- Modify `CHANGELOG.md` — record the enforcement addition.

### Task 1: Typed review records and mandatory-block rules

**Files:**
- Create: `tools/pr_closure/__init__.py`
- Create: `tools/pr_closure/model.py`
- Create: `tools/pr_closure/review.py`
- Create: `tools/schemas/review-record-v1.json`
- Test: `tools/tests/test_review.py`

- [ ] **Step 1: Write failing tests for the review contract**

Create `tools/tests/test_review.py` with cases that assert:

```python
import unittest

from pr_closure.model import Disposition, Severity, Verdict
from pr_closure.review import ReviewValidationError, validate_review


class ReviewValidationTests(unittest.TestCase):
    def valid_record(self):
        return {
            "schema_version": 1,
            "repository": "owner/repo",
            "pr_number": 42,
            "reviewed_commit": "a" * 40,
            "implementer_family": "deepseek",
            "reviewer_family": "claude",
            "verdict": "APPROVED_WITH_FOLLOW_UPS",
            "findings": [{
                "id": "F-1",
                "root_cause": "missing-output-test",
                "severity": "MINOR",
                "disposition": "FOLLOW_UP_ISSUE",
                "scope": "pre_existing",
                "summary": "Operator output lacks a mutation guard",
                "evidence": ["476 tests survive the mutation"],
                "follow_up_issue": 900,
            }],
            "intentionally_not_findings": [],
        }

    def test_accepts_cross_family_approval_with_filed_follow_up(self):
        review = validate_review(self.valid_record())
        self.assertEqual(Verdict.APPROVED_WITH_FOLLOW_UPS, review.verdict)
        self.assertEqual(Disposition.FOLLOW_UP_ISSUE, review.findings[0].disposition)

    def test_rejects_same_family_review(self):
        record = self.valid_record()
        record["reviewer_family"] = "deepseek"
        with self.assertRaisesRegex(ReviewValidationError, "different model family"):
            validate_review(record)

    def test_rejects_follow_up_without_issue(self):
        record = self.valid_record()
        del record["findings"][0]["follow_up_issue"]
        with self.assertRaisesRegex(ReviewValidationError, "follow_up_issue"):
            validate_review(record)

    def test_promotes_central_claim_failure_to_blocker(self):
        record = self.valid_record()
        record["verdict"] = "APPROVED_WITH_FOLLOW_UPS"
        record["findings"][0].update({
            "severity": "MEDIUM",
            "disposition": "FOLLOW_UP_ISSUE",
            "scope": "central_claim",
        })
        review = validate_review(record)
        self.assertEqual(Disposition.BLOCKS_PR, review.findings[0].disposition)
        self.assertEqual(Verdict.CHANGES_REQUIRED, review.verdict)

    def test_rejects_unknown_verdict(self):
        record = self.valid_record()
        record["verdict"] = "LOOKS_FINE"
        with self.assertRaisesRegex(ReviewValidationError, "verdict"):
            validate_review(record)
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```bash
PYTHONPATH=tools python3 -m unittest tools.tests.test_review -v
```

Expected: import failure because `pr_closure` does not exist.

- [ ] **Step 3: Implement enums, immutable records, and validation**

Implement these exact public types in `model.py`:

```python
class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MEDIUM = "MEDIUM"
    MINOR = "MINOR"
    NOTE = "NOTE"


class Disposition(StrEnum):
    BLOCKS_PR = "BLOCKS_PR"
    FOLLOW_UP_ISSUE = "FOLLOW_UP_ISSUE"
    NOTE_ONLY = "NOTE_ONLY"


class Verdict(StrEnum):
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    APPROVED_WITH_FOLLOW_UPS = "APPROVED_WITH_FOLLOW_UPS"
    APPROVED = "APPROVED"
    INCONCLUSIVE = "INCONCLUSIVE"
```

Use frozen dataclasses `Finding` and `ReviewRecord`. In `review.py`, validate required fields,
40-character lowercase hexadecimal commit IDs, unique finding IDs, cross-family review, and issue
numbers for follow-ups. Promote `central_claim`, `acceptance`, `regression`, `security`, `privacy`,
`authorization`, `billing`, `data_integrity`, `ci`, `verification`, and `review_tip` scopes to
`BLOCKS_PR`. Recompute the verdict after promotion.

- [ ] **Step 4: Publish the equivalent JSON schema**

Create `tools/schemas/review-record-v1.json` with `additionalProperties: false`, the enums above,
and conditional `follow_up_issue` requirement when disposition is `FOLLOW_UP_ISSUE`.

- [ ] **Step 5: Run the review tests**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add tools/pr_closure tools/schemas/review-record-v1.json tools/tests/test_review.py
git commit -m "feat: validate structured review outcomes"
```

### Task 2: Pure closure-state derivation

**Files:**
- Modify: `tools/pr_closure/model.py`
- Create: `tools/pr_closure/state.py`
- Test: `tools/tests/test_state.py`

- [ ] **Step 1: Write the state-transition matrix tests**

Define `ClosureState`, `CiState`, and frozen `ClosureSnapshot`. Test at minimum:

```python
def test_branch_ci_failure_wins_over_review(): ...
def test_infra_failure_enters_bounded_retry(): ...
def test_unpushed_commit_is_unverified(): ...
def test_dirty_review_probe_is_unverified(): ...
def test_green_unreviewed_tip_is_review_ready(): ...
def test_reviewed_tip_mismatch_invalidates_approval(): ...
def test_blocking_finding_requires_changes(): ...
def test_missing_follow_up_issue_requires_filing(): ...
def test_approved_with_issues_requires_same_green_tip(): ...
def test_repeated_root_cause_enters_design_reset(): ...
def test_no_progress_inside_budget_enters_stalled(): ...
```

The precedence must be:

```text
missing/contradictory evidence
> unpushed/dirty/tip mismatch
> branch CI red
> infrastructure retry
> fixing/local verification
> active review
> repeated-root design reset
> blocking findings
> follow-up filing
> approved states
> review ready
```

- [ ] **Step 2: Run and observe the missing implementation failure**

```bash
PYTHONPATH=tools python3 -m unittest tools.tests.test_state -v
```

- [ ] **Step 3: Implement `derive_state(snapshot, now)` as a pure function**

Do not call Git, GitHub, Trello, or the filesystem from `state.py`. Return a `StateDecision` with
`state`, ordered `reasons`, and `allowed_actions`. Unknown enum values must raise, not default.

- [ ] **Step 4: Run state tests and the review regression suite**

```bash
PYTHONPATH=tools python3 -m unittest tools.tests.test_state tools.tests.test_review -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add tools/pr_closure/model.py tools/pr_closure/state.py tools/tests/test_state.py
git commit -m "feat: derive fail-closed PR closure states"
```

### Task 3: Durable append-only evidence store

**Files:**
- Create: `tools/pr_closure/store.py`
- Test: `tools/tests/test_store.py`

- [ ] **Step 1: Write failing durability tests**

Test that `RunStore(root, project, pr)`:

- appends newline-delimited JSON events without rewriting prior lines;
- writes verification under `verification/<commit>.json`;
- writes reviews under `reviews/<commit>/<review-id>.json`;
- refuses to overwrite an existing review ID with different bytes;
- ignores `/tmp` and `~/.claude/jobs` as authoritative paths;
- invalidates cached approval when a newer commit event appears; and
- returns `UNVERIFIED` when a referenced artifact is missing.

- [ ] **Step 2: Run and confirm failure**

```bash
PYTHONPATH=tools python3 -m unittest tools.tests.test_store -v
```

- [ ] **Step 3: Implement atomic writes and append-only events**

Use `tempfile.NamedTemporaryFile(dir=target.parent)` plus `os.replace` for record files. Use
`os.open(events, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)` for events. Every event includes
`schema_version`, UTC timestamp, project, PR, commit, event type, and evidence path.

- [ ] **Step 4: Run store, state, and review tests**

```bash
PYTHONPATH=tools python3 -m unittest tools.tests.test_store tools.tests.test_state tools.tests.test_review -v
```

- [ ] **Step 5: Commit Task 3**

```bash
git add tools/pr_closure/store.py tools/tests/test_store.py
git commit -m "feat: persist append-only closure evidence"
```

### Task 4: Fail-closed Git and GitHub sources

**Files:**
- Create: `tools/pr_closure/sources.py`
- Test: `tools/tests/test_sources.py`

- [ ] **Step 1: Write command-runner fixture tests**

Use a fake runner returning `(returncode, stdout, stderr)`. Cover:

- `gh` failure raises `SourceUnavailable` rather than returning no PRs;
- empty or malformed JSON raises `SourceMalformed`;
- local/remote/head/CI commit mismatch is preserved in the snapshot;
- a dirty worktree includes untracked files;
- GitHub `FAILURE` checks are not silently filtered by a green aggregate gate; and
- an infrastructure classification is accepted only when an evidence event names a failed job and
  shows no code-test step started.

- [ ] **Step 2: Run and confirm failure**

```bash
PYTHONPATH=tools python3 -m unittest tools.tests.test_sources -v
```

- [ ] **Step 3: Implement `GitSource` and `GitHubSource`**

Use these commands:

```text
git worktree list --porcelain
git -C <worktree> status --porcelain=v1 --untracked-files=all
git -C <worktree> rev-parse HEAD
git -C <worktree> rev-parse origin/<branch>
gh pr view <pr> --repo <owner/repo> --json number,headRefName,headRefOid,isDraft,state,statusCheckRollup,url
```

Never use `stdout or "[]"`. A failed command, empty output, missing key, or unknown check conclusion
raises a typed source error.

- [ ] **Step 4: Run all unit tests**

```bash
PYTHONPATH=tools python3 -m unittest discover -s tools/tests -v
```

- [ ] **Step 5: Commit Task 4**

```bash
git add tools/pr_closure/sources.py tools/tests/test_sources.py
git commit -m "feat: read closure sources without guessing"
```

### Task 5: Heavy-job lease and lane health

**Files:**
- Create: `tools/pr_closure/lease.py`
- Test: `tools/tests/test_lease.py`

- [ ] **Step 1: Write failing lease tests**

Test that the first process acquires an `fcntl.flock` lease, a second non-blocking acquisition fails
with the owner metadata, stale metadata without a held lock does not block, and release removes the
metadata only when the current process owns it.

- [ ] **Step 2: Run and confirm failure**

```bash
PYTHONPATH=tools python3 -m unittest tools.tests.test_lease -v
```

- [ ] **Step 3: Implement `HeavyJobLease`**

Store PID, command, project, PR, and start time next to the lock. The lock—not the PID file—is the
authority. Add `is_lane_live(previous, current)` that returns true only when output bytes grow or CPU
time advances.

- [ ] **Step 4: Run lease tests twice to catch leaked locks**

Run the Step 2 command twice. Expected: both runs pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add tools/pr_closure/lease.py tools/tests/test_lease.py
git commit -m "feat: serialize heavy closure jobs"
```

### Task 6: CLI and project configuration

**Files:**
- Create: `tools/pr_closure/cli.py`
- Create: `tools/pr-closure`
- Create: `tools/schemas/project-closure-v1.json`
- Test: `tools/tests/test_cli.py`

- [ ] **Step 1: Write CLI fixture tests**

Cover exact exit behavior:

```text
0  requested read/check succeeded
2  invalid CLI/config/review input
3  source unavailable or malformed
4  requested transition denied
5  verification command failed
6  heavy-job lease unavailable
```

Test `status --json`, `import-review`, `record-verification`, `record-infra-failure`,
`check-transition`, and `sync --dry-run`. Assert that `sync` refuses all writes when any source is
unavailable.

- [ ] **Step 2: Run and confirm failure**

```bash
PYTHONPATH=tools python3 -m unittest tools.tests.test_cli -v
```

- [ ] **Step 3: Implement versioned project config**

The JSON config contains:

```json
{
  "schema_version": 1,
  "project": "example",
  "repository": "owner/repo",
  "repo_path": "/absolute/path",
  "default_branch": "develop",
  "closure_state_dir": "/absolute/durable/path",
  "local_review_ready_commands": ["command"],
  "closure_acceptance_commands": ["command"],
  "infra_retry_budget": 1,
  "stagnation_budget_minutes": 240,
  "heavy_job_limit": 1,
  "tracking_projection": null
}
```

Reject relative repo/state paths, unknown keys, empty command lists, non-positive budgets, and
unsupported schema versions.

- [ ] **Step 4: Implement CLI commands**

`status` only reads. `record-verification` runs configured commands under the heavy-job lease and
records each exit code. `import-review` validates before storing. `check-transition` derives live
state and compares the requested transition. `sync` prints planned projection changes by default;
`--apply` requires all sources green and an adapter projection command.

- [ ] **Step 5: Run CLI and full tests**

```bash
PYTHONPATH=tools python3 -m unittest discover -s tools/tests -v
tools/pr-closure --help
```

Expected: tests pass and help exits 0.

- [ ] **Step 6: Commit Task 6**

```bash
git add tools/pr_closure/cli.py tools/pr-closure tools/schemas/project-closure-v1.json tools/tests/test_cli.py
git commit -m "feat: add PR closure gate CLI"
```

### Task 7: Playbook and template integration

**Files:**
- Modify: `PLAYBOOK.md`
- Modify: `adapter-template.md`
- Modify: `captain-packet-template.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the normative closure section to the playbook**

Condense the approved design into a new section after the review loop. Include the state table,
mandatory blockers, two-axis findings, exact verdicts, stale-approval rule, design-reset rule,
stagnation rule, sources of truth, and `pr-closure check-transition` as a mandatory precondition.

- [ ] **Step 2: Add closure fields to the adapter template**

Add `closure_config`, `review_publication_cmd`, and the design's project-specific fields. State that
missing fields are a preflight stop for orchestrated PR work.

- [ ] **Step 3: Update packet and reviewer templates**

Add exact pushed commit, blocking finding IDs, root-cause history, paired proof, structured JSON
output, and “brief statements found wrong” to implementation returns. Add severity, disposition,
scope, evidence, and follow-up issue to review returns.

- [ ] **Step 4: Document installation and migration**

README commands:

```bash
PYTHONPATH="$HOME/ai-orchestration-playbook/tools" \
  "$HOME/ai-orchestration-playbook/tools/pr-closure" status \
  --config /absolute/project-closure.json --pr 123
```

Explain that existing free-form verdicts must be imported once; they never silently count as
approval.

- [ ] **Step 5: Run documentation checks**

```bash
git diff --check
rg -n 'TBD|TODO|PLACEHOLDER' PLAYBOOK.md adapter-template.md captain-packet-template.md README.md CHANGELOG.md
```

Expected: `git diff --check` exits 0; placeholder search returns no matches introduced by this task.

- [ ] **Step 6: Commit Task 7**

```bash
git add PLAYBOOK.md adapter-template.md captain-packet-template.md README.md CHANGELOG.md
git commit -m "docs: require mechanical PR closure gates"
```

### Task 8: Paid-failure regression fixtures

**Files:**
- Create: `tools/tests/fixtures/digital_prevention/`
- Create: `tools/tests/fixtures/publyapp/`
- Modify: `tools/tests/test_cli.py`
- Modify: `tools/tests/test_state.py`

- [ ] **Step 1: Encode sanitized Digital Prevention fixtures**

Include:

- API failure previously read as no PR;
- one-line verdict versus two-axis verdict drift;
- milestone approval incorrectly treated as PR approval;
- approved review with a pre-existing audit gap filed as follow-up;
- current-branch accounting regression that must block;
- missing temporary harness directory; and
- Trello claiming approved while GitHub remains draft.

- [ ] **Step 2: Encode sanitized PublyApp fixtures**

Include:

- unpushed fix reviewed as absent;
- exit-zero empty worker result;
- verification pipeline that captured the wrong exit code;
- branch-caused lint failure;
- browser-cache infrastructure failure before tests start;
- central guard claim silently defaulting undecidable to safe; and
- same root cause surviving two syntax-instance fixes.

- [ ] **Step 3: Add parameterized regression tests**

Every fixture declares `expected_state`, `expected_blocking_ids`, and `must_not_state`. The test loops
over both directories and asserts the derived state.

- [ ] **Step 4: Demonstrate fail-closed mutation strength**

Temporarily change one source failure to return an empty list and show the API-failure fixture fails.
Restore it. Temporarily disable mandatory-block promotion and show the central-claim fixture fails.
Restore it.

- [ ] **Step 5: Run the complete framework suite**

```bash
PYTHONPATH=tools python3 -m unittest discover -s tools/tests -v
git diff --check
```

Expected: all tests pass and no whitespace errors.

- [ ] **Step 6: Commit Task 8**

```bash
git add tools/tests
git commit -m "test: pin paid PR closure failures"
```

### Task 9: Migrate every canonical existing project adapter

**Files:**
- Modify: `/home/radan/Projects/DigitalPrevention/digital-prevention/.ai/orchestration-adapter.md`
- Modify: `/home/radan/Projects/PublyApp/publyapp/.ai/orchestration-adapter.md` through the dependent PublyApp adoption plan
- Modify: `/home/radan/Projects/Tosin4dev/tosin4dev/.ai/orchestration-adapter.md`
- Test: adapter contract audit from the framework CLI

- [ ] **Step 1: Write a failing canonical-adapter audit test**

Add a CLI test for `audit-adapters --roots <path>...`. It must ignore nested `.worktrees/` and
secondary numbered clones, find each canonical project's adapter, and fail when any adapter lacks
`closure_config`, `closure_gate`, `review_record_schema`, `review_publication_cmd`, or
`tracking_projection`.

- [ ] **Step 2: Implement the read-only adapter audit**

The command reports every missing field and exits 4 if any adapter is unmigrated. It never edits a
repository. Add the command to the playbook preflight so an old adapter fails closed instead of
silently using the legacy review loop.

- [ ] **Step 3: Create one isolated migration branch per canonical repository**

Follow each repository's protected-branch and worktree rules. Do not edit default-branch checkouts.
DigitalPrevention migration is explicitly authorized by the owner's all-project request, but still
uses a dedicated PR and carries no temporary logs. PublyApp migration is Task 9 of the dependent
adoption plan. Do not edit numbered clones or existing worktrees separately; they inherit the
canonical branch change after normal synchronization.

- [ ] **Step 4: Add project-specific closure configuration bindings**

Keep commands, heavy-job limits, model ladders, and tracking targets project-specific. Do not copy
PublyApp's Node/.NET/e2e commands into DigitalPrevention or Tosin4dev. The common invariant is only
the mechanical gate and structured review contract.

- [ ] **Step 5: Verify each adapter and run the global audit**

Run each repo's focused documentation/adapter check, then:

```bash
tools/pr-closure audit-adapters \
  --roots /home/radan/Projects/DigitalPrevention/digital-prevention \
          /home/radan/Projects/PublyApp/publyapp \
          /home/radan/Projects/Tosin4dev/tosin4dev
```

Expected: exit 0 only after all three canonical adapters bind the closure gate. Open reviewable PRs
where required; do not merge them.

### Task 10: Final framework verification

**Files:**
- Verify only; no planned code changes.

- [ ] **Step 1: Run all tests from a clean shell**

```bash
PYTHONPATH=tools python3 -m unittest discover -s tools/tests -v
```

- [ ] **Step 2: Run CLI smoke tests**

```bash
tools/pr-closure --help
tools/pr-closure status --config tools/tests/fixtures/publyapp/green-reviewed.json --pr 1078 --offline
```

Expected: help exits 0; offline fixture reports its expected approval state.

- [ ] **Step 3: Verify repository hygiene**

```bash
git diff --check
git status --short
```

Expected: only pre-existing, explicitly preserved user files remain untracked; no framework changes
are uncommitted.

- [ ] **Step 4: Run an independent cross-family review**

Review the complete branch against the design. Findings must use the new structured review schema.
Any blockers return to Task 1-8 as appropriate; non-blockers require verified follow-up issues.

- [ ] **Step 5: Record readiness without merging**

Run `tools/pr-closure check-transition ... --to APPROVED` against the framework's own fixture-backed
review record. Report the artifact path. Do not push or merge without explicit owner authorization.
