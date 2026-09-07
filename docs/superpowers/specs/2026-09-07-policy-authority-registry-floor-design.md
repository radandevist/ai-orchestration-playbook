# Immutable Project Policy Floor Design

**Date:** 2026-09-07
**Status:** Approved design reset
**Scope:** Shared `tools/pr_closure` gate, with an exact floor for `PublyApp/publyapp`

## Goal

Make the versioned immutable registry, rather than rollbackable project events, the minimum
review-policy authority for `PublyApp/publyapp`. The floor must remain enforced when local state is
empty, unadopted, truncated, copied, or replaced, while unknown repositories retain generic
explicit-configuration behavior.

## Authority boundary

`tools/pr_closure/registries.py` will contain a code-reviewed, immutable repository-policy floor
registry. Its exact `PublyApp/publyapp` entry binds the mandatory policy ID and digest (and the
canonical policy definition where needed) plus the complete authorized routes: implementations
per current configuration, exact GPT-5.6 Luna to GPT-5.6 Sol owner-authorized same-OpenAI-family
exception, and no Claude authority. The registry is versioned and checked by a released golden
digest, like the existing model and launcher registries.

The floor is applied by configuration normalization and by every authority consumer. A local
config may equal or tighten a registered floor, but cannot omit, disable, weaken, or replace it.
The floor is still enforced if the local store has no adoption event or if adoption events,
`events.jsonl`, the append journal, or external anchor are rolled back. Project adoption events
remain audit/migration evidence and may document or tighten state; they never create, remove, or
weaken the compiled floor.

Repositories without a registered floor continue using the existing generic explicit-config
behavior, including legacy schema-v1 compatibility when no policy is configured.

## Stream integrity boundary

The external event-stream anchor remains only where it provides append integrity and crash
recovery. Its code and documentation must not claim that it supplies monotonic policy authority
against rollback of the complete local trust set. Prefer deleting adoption-authority coupling and
related complexity; retain event validation and crash recovery needed by the existing suite.

## Enforcement flow

1. `validate_project_config` resolves the immutable floor for the repository and rejects a
   missing, disabled, weaker, or mismatched local policy before any review can be authoritative.
2. `validate_review` receives the effective policy/routes and enforces exact model, runner,
   launcher, provenance, exception, and no-Claude checks as before.
3. `status` derives authority from the effective floor plus valid current evidence, not from the
   presence of an adoption event.
4. `import-review` applies the same effective floor before writing any review artifact.
5. Adoption and activation checks validate audit consistency but are never the only prerequisite
   for the registered repository's policy.

## Tests and mutation proof

RED tests cover: rollback of valid event/head prefixes with inode preservation; restoration of a
saved pre-adoption PR directory and anchor directory; fresh PublyApp state with removed,
disabled, or weakened policy; the exact Luna-to-Sol route; generic unknown-repository behavior;
and a mutation that bypasses registry-floor lookup. GREEN tests must show the first four remain
fail-closed and the generic case remains compatible. The mutation test must fail when floor lookup
is bypassed.

## Documentation

Update `PLAYBOOK.md`, `README.md`, and the PublyApp rollout/design plan language so that owner-
mandated Sol-only adversarial reviews at high/xhigh are explicit, the versioned registry is the
project policy floor, and event adoption is audit/migration evidence rather than the sole latch.
