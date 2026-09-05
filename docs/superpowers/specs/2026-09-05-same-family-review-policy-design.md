# Same-Family Review Policy Design

**Date:** 2026-09-05  
**Status:** Design specification  
**Scope:** Shared `tools/pr_closure` gate, with the PublyApp policy as the first adopter

## Goal

Make the review-family rule express the owner-approved PublyApp exception precisely:

- Claude is not an allowed new reviewer for PublyApp.
- A GPT-family implementation may be reviewed by `gpt-5.6-sol`.
- That Luna-to-Sol route is a named, auditable same-family exception rather than a silent
  weakening of the shared rule.
- Every other project and every unconfigured route continues to reject same-family review.

The gate remains fail-closed. A missing policy, malformed policy, unknown model, missing
exception declaration, stale artifact, or contradictory family claim cannot produce approval.

## Current contract and constraints

`validate_review()` currently resolves `implementer_family` and `reviewer_family`, then rejects
equal resolved families. `ProjectConfig` has no review policy. `cmd_import_review()` and the
read-only `status` path both validate durable review records, so they must consume the same
policy object.

The existing field names are misleading in practice: durable records frequently put model IDs
such as `gpt-5.6-sol` and `claude-sonnet-5` in fields named `*_family`. The new contract must not
pretend that an arbitrary label proves a family. It must derive lineage from canonical model IDs.

The store is immutable and `bound_reviews()` reads every review artifact bound to the current tip.
Writing a replacement next to an obsolete artifact is therefore insufficient: the obsolete
artifact would still be parsed and could still block status derivation.

## Decision

Add an optional, strictly validated `review_policy` to project configuration. Keep the generic
default empty, so existing projects retain the current cross-family-only rule. Add an optional
exception declaration to the review artifact. The artifact can request an exception, but only the
project configuration can authorize it.

The policy is additive to project-configuration schema version 1. Review artifacts move to schema
version 2 because mandatory provenance is a safety boundary, not an optional decoration. Schema-v1
artifacts remain parseable for historical inspection and migration diagnostics, but are not
current approval evidence for a project with an active policy. Unknown fields remain rejected.

### Configuration API

The Python API adds immutable values equivalent to:

```python
@dataclass(frozen=True)
class SameFamilyReviewException:
    id: str
    implementer_models: tuple[str, ...]
    reviewer_model: str
    owner_authorization: str
    rationale: str

@dataclass(frozen=True)
class ReviewPolicy:
    forbidden_reviewer_families: tuple[str, ...] = ()
    same_family_exceptions: tuple[SameFamilyReviewException, ...] = ()
```

`ProjectConfig.review_policy` is optional and defaults to an empty `ReviewPolicy`. The JSON
shape is:

```json
{
  "review_policy": {
    "forbidden_reviewer_families": ["anthropic"],
    "same_family_exceptions": [
      {
        "id": "publyapp-gpt-implementation-sol-review-v1",
        "implementer_models": ["gpt-5.6-luna"],
        "reviewer_model": "gpt-5.6-sol",
        "owner_authorization": "Radan; owner instruction 2026-09-05",
        "rationale": "GPT implementation is reviewed by gpt-5.6-sol; Claude is forbidden."
      }
    ]
  }
}
```

Configuration validation is fail-closed:

- `review_policy` must be an object; its keys are exact.
- Family names are normalized through the existing resolver and must be known.
- Exception IDs are non-blank, unique, and stable path-safe identifiers.
- Model IDs are non-blank raw canonical registry keys with no whitespace; each must resolve to a
  known family. Trimming or case-folding an input is not normalization and cannot make it valid.
- `implementer_models` is non-empty and unique.
- `reviewer_model`, `owner_authorization`, and `rationale` are non-blank.
- The exception's model families must be equal; otherwise it is not a same-family exception.
- An exception cannot authorize a reviewer family listed in `forbidden_reviewer_families`.
- Duplicate exceptions or duplicate forbidden families are rejected.
- No policy is inferred from the project name, repository name, environment, or reviewer text.

#### Canonical model identity registry

Family resolution is not a model registry. It can recognize a lineage from a spelling, but it
cannot prove that the spelling names a routable model. The shared gate therefore owns a versioned,
code-reviewed registry of exact canonical model IDs:

```python
MODEL_REGISTRY_V1 = {
    "gpt-5.6-luna": "openai",
    "gpt-5.6-sol": "openai",
    "gpt-5.6-terra": "openai",
    "gpt-5.3-codex-spark": "openai",
    "deepseek-v4-flash": "deepseek",
    "deepseek-v4-pro": "deepseek",
    "glm-5.2": "zhipu",
    "qwen3.7-max": "alibaba",
    "qwen3.7-plus": "alibaba",
    "kimi-k2.6": "moonshot",
    "kimi-k2.7-code": "moonshot",
    "kimi-k3": "moonshot",
    "minimax-m3": "minimax",
    "mimo-v2.5": "xiaomi",
    "mimo-v2.5-pro": "xiaomi",
    "claude-opus-5": "anthropic",
    "claude-sonnet-5": "anthropic",
    # Every supported model is listed explicitly; no heuristic fallback exists.
}
MODEL_ALIASES_V1 = {
    # Only reviewed aliases map to canonical IDs; family words such as "gpt" are not aliases.
}
```

The real registry is complete for every model accepted by the lane adapter; the abbreviated
snippet above is illustrative, not a permission to accept an unlisted ID. For schema-v2 records
and configuration, the raw `model_id` must equal a registry key byte-for-byte: there is no trim,
case-fold, Unicode normalization, punctuation folding, or implicit alias lookup. This keeps the
serialized identity auditable and prevents two spellings from becoming the same reviewer by
accident. It rejects internal or surrounding whitespace, control characters, path separators,
empty values, unknown IDs, and labels that identify only a family (`gpt`, `openai`, `claude`, or
`anthropic`). Registry entries map to one immutable family.

Aliases exist only in a separately scoped `legacy-v1` reader for historical records. They are
explicit, one-to-one, versioned mappings used to inventory or migrate old metadata; they are never
accepted in schema-v2/configuration and never satisfy a same-family exception. The legacy reader
retains both the original spelling and the resulting canonical key in its migration report.

The config validator resolves every exception model through this registry. An unknown ID,
ambiguous alias, family mismatch, or legacy declaration that disagrees with the registry is a
configuration error. The registry version is included in the normalized policy and in the review
provenance, so changing a model's family cannot silently validate an old artifact.

For PublyApp, the exact policy above is the only same-family exception. It deliberately names
`gpt-5.6-luna` and `gpt-5.6-sol`, rather than allowing every future GPT model implicitly. The
adapter's new-review lanes must remove Claude entirely and route a GPT implementation to Sol.

### Review validation API

Change the validator to:

```python
validate_review(record, *, review_policy: ReviewPolicy | None = None) -> ReviewRecord
```

`None` is normalized to an empty policy. The validator performs these checks in this order:

1. Validate the existing record shape and exact schema version.
2. Resolve both raw model IDs through the versioned registry. New records use the model fields
   below and require exact registry keys; a legacy record may use its old `*_family` values through
   the explicitly scoped `legacy-v1` alias reader for inventory only. Family-only legacy labels are
   historical metadata, not current model provenance.
3. Derive each family from the registry entry. If any supplied legacy family label, explicit family,
   or model field disagrees with that result, reject the record, even when both values happen to be
   in the same lineage. Never accept a same-lineage record merely because its labels agree.
4. Reject a reviewer whose resolved family is forbidden by the project policy.
5. If the resolved families differ, accept the normal cross-family path and reject any attached
   same-family exception declaration.
6. If the resolved families are equal, reject unless a complete `review_exception` is present and
   exactly matches one configured exception ID, implementer model, and reviewer model.
7. Validate both provenance envelopes against the durable source and digest contract below.
8. Preserve all existing finding, live-tip, and verdict validation.

The default branch of this algorithm still rejects equal families. A policy is an allowlist of
exact pairs, not a boolean bypass.

### Review artifact shape

Add these fields to generated `review-record-v2.json`:

```json
{
  "implementer_model": "gpt-5.6-luna",
  "reviewer_model": "gpt-5.6-sol",
  "review_exception": {
    "policy_id": "publyapp-gpt-implementation-sol-review-v1"
  },
  "provenance": {
    "registry_version": "models-v1",
    "implementer": {
      "model_id": "gpt-5.6-luna",
      "run_ref": "orchestration://run/2026-09-05/luna-123",
      "durable_path": "/home/radan/.hermes/orchestration/closure/publyapp/2104/provenance/impl.json",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    },
    "reviewer": {
      "model_id": "gpt-5.6-sol",
      "run_ref": "orchestration://run/2026-09-05/sol-456",
      "durable_path": "/home/radan/.hermes/orchestration/closure/publyapp/2104/provenance/review.json",
      "sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    }
  }
}
```

Rules:

- `implementer_model` and `reviewer_model` are required together when either is present.
- `review_exception` is required for same-family records and forbidden for cross-family records.
- `review_exception` has exactly one key, `policy_id`.
- The policy ID must be present in the loaded project config; an artifact cannot create or amend
  policy.
- `provenance` is mandatory for every newly imported schema-v2 review, not only for the exception
  path. Its keys are exact: `registry_version`, `implementer`, and `reviewer`.
- Each provenance participant has exactly `model_id`, `run_ref`, `durable_path`, and `sha256`.
  `model_id` must equal the corresponding artifact model field byte-for-byte and resolve directly
  through the declared registry version. `run_ref` is a non-blank immutable lane/run identifier
  that must resolve to a durable run-manifest entry under the configured closure root; the
  manifest must name the same repository, PR, reviewed commit, canonical model ID, and output
  digest. A string that is merely plausible or supplied only in the review JSON is not a valid run
  reference.
- `durable_path` must be absolute, resolve without a symlink escape, be a regular readable file
  under the configured durable closure root, and not live under a temporary session directory.
  The importer reads its bytes and requires the lowercase 64-hex `sha256` to match. Status repeats
  the path, regular-file, root, and digest checks on every read; missing, replaced, or inaccessible
  provenance is `UNVERIFIED`, never an approval.
- The provenance file is an immutable lane envelope containing the run ref, canonical model ID,
  registry version, reviewed commit, and producer output digest. There are two mandatory halves:
  one immutable implementer-lane output and one immutable reviewer-lane output. The referenced run
  manifests and both envelopes are read and digest-checked at import and status. The review JSON
  may reference them, but cannot replace either half with `local_evidence` text.
- For a same-family exception, the model fields are mandatory even when legacy family fields are
  also present. The validator derives the family from these model fields and checks every duplicate
  declaration for consistency.
- New producers must populate model fields. Legacy records remain valid only under the existing
  cross-family rule and cannot use a same-family exception without the new fields.

The normalized `ReviewRecord` stores `schema_version=2`, `implementer_model`, `reviewer_model`, the resolved family
values, registry version, provenance descriptors, and the optional exception ID. The raw JSON
remains byte-preserved in the durable store; normalization never rewrites evidence. Schema-v1
records can be read for historical inspection, but a project with an active `review_policy` cannot
use a schema-v1 record as current-tip approval because it lacks mandatory provenance.

This is honest about what the artifact proves: it records the model IDs claimed by the lane and
binds them to the reviewed tip, but it does not claim cryptographic proof of provider execution.
The lane output or evidence reference must identify the invocation/run that produced the artifact.
An arbitrary text assertion such as `reviewer_family: "openai"` is never sufficient to establish
that `gpt-5.6-sol` actually reviewed the commit.

## Import and status consistency

`cmd_import_review()` passes `config.review_policy` to `validate_review()` before writing any
artifact. The status path passes the same object while parsing every result from
`store.bound_reviews()`. No second policy implementation is permitted.

The policy is loaded and validated once with `ProjectConfig`; its normalized immutable value is
passed through both paths. A policy change invalidates no bytes and silently upgrades no record.
If a durable record or its provenance no longer satisfies the active policy, status returns a typed
malformed/unverified error rather than treating another review or a projection as approval
evidence. Import validates provenance before creating the review event, and status validates the
same envelope, path, registry version, model IDs, reviewed commit, and digest before considering
the review verdict.

The public schema and Python validator intentionally retain their existing semantic asymmetry:
JSON Schema cannot compare resolved families or look up policy IDs, so those checks belong to the
Python gate and are listed in the schema's generated semantic-asymmetry comment.

## Durable retirement of obsolete review artifacts

Because `bound_reviews()` currently treats every direct JSON artifact as authoritative input, policy
migration needs a first-class retirement operation. It must not delete or overwrite evidence.

Add a store operation with this contract:

```text
retire-review --pr <N> --commit <40-hex> --review-id <id> \\
  --retirement-id <id> --reason <reason> --policy-id <policy-id> \\
  --expected-sha256 <64-hex>
```

The operation is internal to the closure CLI/API and is only allowed for an exact, existing,
event-bound review artifact. The arguments are captured in an immutable retirement envelope:

```json
{
  "schema_version": 1,
  "operation": "REVIEW_RETIREMENT",
  "retirement_id": "retire-2026-09-05-pr2104-sol-v1",
  "repository": "PublyApp/publyapp",
  "project": "publyapp",
  "pr_number": 2104,
  "commit": "<40 lowercase hex>",
  "review_id": "review-pr2104-sol",
  "source_path": "<resolved active review path>",
  "expected_sha256": "<64 lowercase hex>",
  "reason": "policy-migration: same-family-review-replaced",
  "policy_id": "publyapp-gpt-implementation-sol-review-v1",
  "requested_at": "<UTC timestamp>"
}
```

`retirement_id`, reason, and policy ID are required; IDs are unique and path-safe, reasons are
non-blank and policy-scoped, and the expected digest is mandatory. The operation is idempotent only
when a repeated request has byte-identical envelope arguments. A different reason, policy, source,
or digest for the same retirement ID is a hard conflict.

The state machine is `ACTIVE -> PREPARED -> COPIED -> COMMITTED -> FINALIZED`. The operation:

1. reads and hashes the original immutable bytes;
2. atomically writes and fsyncs the retirement envelope in a durable `retirements/<commit>/`
   namespace, producing `PREPARED`;
3. copies the original bytes to a unique `reviews-retired/<commit>/<retirement-id>.json` path,
   fsyncs the file and directory, re-hashes the copy, and records `COPIED`;
4. appends and fsyncs a `REVIEW_RETIREMENT_COMMITTED` event containing both paths, all envelope
   arguments, and the matching digest;
5. atomically renames the active artifact to the retired path (or confirms that the exact rename
   already happened), fsyncs both parent directories, and records `FINALIZED`.

`bound_reviews()` excludes only a retirement with a valid `FINALIZED` envelope/event/path/digest
relation. `PREPARED`, `COPIED`, or `COMMITTED` states make status return `UNVERIFIED`; they may
never be interpreted as either active approval or completed retirement. A recovery pass resumes a
valid in-progress envelope from its last durable state, verifies every digest before advancing,
and refuses conflicting or incomplete state. A crash before the committed event leaves the active
review authoritative; a crash after it leaves the PR unverified until recovery finishes or the
owner explicitly aborts the retirement. `validate_event_relations()` validates the complete
state machine, envelope equality, source/target containment, event order, and byte digests.

The move and event append must be crash-safe and idempotent. A missing event, changed digest,
foreign path, duplicate incompatible retirement, or partially moved artifact fails closed. The
retirement reason for the PublyApp rollout is `policy-migration: claude-reviewer-forbidden`; it
does not claim that the historical review was invalid under the old policy.

Migration of an active tip is therefore:

1. keep the old Claude artifact immutable;
2. obtain a fresh allowed review at the exact same pushed tip;
3. import the new artifact with its real model IDs and, for Luna→Sol, the exact exception ID;
4. retire the old Claude artifact with a durable retirement event;
5. run `status` and confirm only the new active review contributes to the verdict.

Historical closed PRs need no rewrite. Their old artifacts remain in the retired or historical
namespace and are not eligible as current-tip authority. No migration step may edit JSON bytes in
place, invent a Sol review, or delete evidence.

## Tests

### Contract and schema

- policy absent preserves the current default;
- valid PublyApp policy parses to normalized immutable values;
- the complete `models-v1` registry contains every active adapter model exactly once, and no
  unlisted model ID or family-only label validates;
- schema-v2/config accepts only byte-exact lowercase registry keys; surrounding whitespace,
  case variants, Unicode lookalikes, and punctuation variants are rejected;
- legacy aliases are accepted only by the explicitly scoped inventory reader and never by a
  same-family exception;
- blank, duplicate, unknown, or conflicting policy fields are rejected;
- a forbidden reviewer family in an exception is rejected;
- schema/Python agreement covers project policy and the schema-v2 artifact/provenance fields;
- schema-v1 is accepted only by the explicit historical/migration reader and cannot become
  current-tip approval under an active policy;
- missing provenance, wrong registry version, unknown run ref, path escape, symlink, missing file,
  changed bytes, or SHA-256 mismatch is rejected by both import and status;
- implementer and reviewer provenance are both mandatory; an artifact with only reviewer output
  cannot import;
- JSON Schema semantic-asymmetry comments name policy lookup, family comparison, and canonical
  model resolution as Python-only checks.

### Review validator

- same family with no policy is rejected;
- same family with a policy ID absent from config is rejected;
- Luna→Sol with the exact configured ID is accepted;
- a different GPT implementation, reviewer, or policy ID is rejected;
- a registry alias normalizes only to its one explicit canonical ID; an ambiguous alias is rejected;
- a legacy alias or family-only legacy declaration cannot satisfy the Luna→Sol exception;
- same-family with missing model fields or missing exception is rejected;
- cross-family with an exception is rejected;
- Claude reviewer is rejected under the PublyApp policy;
- GPT→DeepSeek and DeepSeek→GPT remain ordinary cross-family approvals;
- model/family mismatches, unknown model IDs, lookalikes, and case/whitespace tricks are rejected;
- all existing findings, mandatory scopes, duplicate IDs, verdict promotion, and live binding tests
  remain green.

### CLI and state

- import applies the policy before any write;
- status applies the identical policy to durable artifacts;
- an accepted same-family artifact cannot be read as approved when the config exception is removed;
- a legacy cross-family artifact remains readable;
- an old Claude artifact plus a new Sol artifact fails until the old artifact is explicitly retired;
- rollout inventory catches every schema-v1 artifact at the active tip, including old
  cross-family records and records with no Claude;
- missing or mutable implementer output forces a fresh implementation verification run, and no
  migration path can manufacture implementer provenance from the review JSON;
- retirement preserves the original digest and event relation;
- retirement envelopes require exact args, reason, policy ID, expected digest, and a unique ID;
- crashes injected before and after every retirement state transition recover idempotently, while
  an incomplete state remains `UNVERIFIED`;
- missing, forged, duplicate, or conflicting retirement events fail closed;
- a new commit or reviewed-tip mismatch still invalidates all review authority.

## Rollout and rollback

Rollout is two phases:

1. ship the shared optional policy, validator, generated schemas, retirement primitive, and tests;
2. update PublyApp's config and adapter routing, then migrate only active tips by fresh review plus
   explicit retirement.

Before enabling the PublyApp policy, inventory current active-tip artifacts and record which ones
are schema-v1, regardless of whether they mention Claude. The inventory has two required halves for
every artifact at every active PR tip: implementer provenance and reviewer provenance. It records
the original JSON digest, both model declarations, whether each side has an immutable lane output,
and the exact replacement/retirement decision. The inventory is advisory; the gate remains
authoritative. Do not enable the policy while any active-tip schema-v1 artifact remains an
unmigrated source of approval.

For each active-tip schema-v1 artifact, migrate in this order:

1. Locate the immutable implementer-lane output and reviewer-lane output. Each output must have a
   durable run manifest, exact reviewed commit, canonical model ID, and independently verifiable
   digest. A chat transcript, model-family label, mutable worktree file, or review JSON alone is
   not an implementer output.
2. If either output exists and its digest verifies, create the schema-v2 provenance envelopes from
   those bytes without changing them. If the implementer output is missing, mutable, or cannot be
   digest-verified, do not fabricate provenance from the old review: start a fresh implementation
   verification run that produces a new immutable implementer output and digest at the exact tip.
   The same rule applies to a missing reviewer output.
3. Obtain a fresh schema-v2 review whenever the old artifact cannot be upgraded from both verified
   immutable halves. Import the fresh review only after both provenance sides validate.
4. Retire the old schema-v1 artifact with the durable retirement protocol, then verify `status`.

Only after every active-tip schema-v1 artifact has a replacement or an explicitly verified
historical retirement may the PublyApp `review_policy` be enabled. This includes old cross-family
records; the policy rollout is not a Claude-only cleanup.

Rollback is a verified sequence, not a config edit:

1. At the exact current pushed tip, obtain a fresh non-Claude, cross-family review with schema-v2
   provenance. For a GPT implementation, use an explicitly non-Claude cross-family reviewer (for
   example DeepSeek) during rollback; do not invent a same-family exception after deciding to roll
   back.
2. Import that review and verify its policy-free cross-family validation, provenance digest, local
   gates, CI, and live-tip binding.
3. Retire the old same-family review with an immutable retirement envelope and complete the
   `ACTIVE -> PREPARED -> COPIED -> COMMITTED -> FINALIZED` protocol. Retire any other active review
   that is forbidden by the policy before removing the policy declaration.
4. Run `status` and require a green state whose only active review authority is the new non-Claude
   cross-family artifact.
5. Only after that status proof succeeds, remove the PublyApp `review_policy` field and run `status`
   again. The second status must still be green under the empty default policy.

If any step fails, restore the policy declaration and leave all artifacts/events untouched; never
delete the replacement or reactivate retired evidence automatically. Retired artifacts remain
retired and are never silently reactivated. Re-activation requires a deliberate, separately audited
restore operation that verifies the original digest and records a new event; it is not part of
ordinary rollback.

## Non-goals

- A global `allow_same_family` switch.
- Treating provider host (`cline-pass`) as a model family.
- Treating a chat/Trello approval or a free-form reviewer name as model provenance.
- Rewriting historical review JSON.
- Automatically merging or approving a PR.
- Relaxing any existing tip, CI, finding, follow-up, or evidence checks.
