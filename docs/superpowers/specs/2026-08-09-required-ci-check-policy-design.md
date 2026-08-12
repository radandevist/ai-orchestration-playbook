# Required CI Check Policy Design

## Goal

Let a project explicitly declare which GitHub check names are authoritative for PR closure, so
intentional optional/skipped checks do not make a healthy PR permanently undecidable.

## Decision

Add an optional `ci_required_checks` array to project-closure configuration.

- When absent or empty, retain the existing conservative behavior: every observed check participates
  in CI classification, so skipped and neutral results cannot count as passing evidence.
- When non-empty, use only checks whose names exactly match the declared list. Each declared name
  must be present exactly once in the live rollup and must conclude `SUCCESS`; a missing,
  duplicate, pending, skipped, neutral, cancelled, or failed declared check remains non-passing.
- Checks outside the declared list are recorded by GitHub but are non-authoritative. They cannot
  turn a passing required set green or red.
- Configuration rejects blank and duplicate names. A typo fails closed as a missing required check.

For PublyApp, declare the existing aggregate gates and the linked-issue gate. The aggregate gates
are authoritative because they already verify their own changed-path prerequisites and downstream
jobs. Shard, cleanup, and optional documentation/spec checks remain visible on GitHub but do not
independently decide closure.

## Data flow

`load_project_config` validates and normalizes `ci_required_checks` into `ProjectConfig`.
`_build_snapshot` passes that immutable policy to `classify_ci`. The classifier first selects the
declared names, verifies one live result per name, then applies the existing state precedence to
the selected results. No fallback to an aggregate check or missing check is allowed.

## Safety properties

- No implicit ignore-list exists; projects must name every authority they rely on.
- A required check that is absent, duplicated, or non-success is `UNKNOWN`, `PENDING`, or
  `BRANCH_FAILURE` as appropriate, never `PASSING`.
- The default for existing projects does not become less strict.
- Infra reclassification remains limited to exactly one selected failed check backed by a durable
  infrastructure event.

## Verification

Tests cover schema/Python agreement, blank/duplicate configuration rejection, exact selection,
missing required checks, duplicate required results, skipped non-authoritative checks, and the
existing all-rollup default. A live PublyApp status must classify its current green aggregator set
as `PASSING` while red selected gates remain `BRANCH_FAILURE`.
