# Same-Family Review Policy Design

**Date:** 2026-09-05  
**Status:** Design specification  
**Scope:** Shared `tools/pr_closure` gate, with the PublyApp policy as the first adopter

## Goal

Make the review-family rule express the owner-approved PublyApp exception precisely:

- Claude is not an allowed new reviewer for PublyApp.
- Every model explicitly authorized as a GPT-family implementer for PublyApp is reviewed by the
  exact reviewer model `gpt-5.6-sol`.
- That GPT-to-Sol route is a named, auditable same-family exception rather than a silent weakening
  of the shared rule. The policy enumerates the complete authorized GPT implementer set; prose or
  family inference cannot widen it.
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
current approval evidence for a project with a staged or enforced policy. When `review_policy` is
absent or empty, normal import and status retain the existing schema-v1 authority path unchanged:
validate
the exact v1 shape, resolve `implementer_family` and `reviewer_family` with the existing family
resolver, and accept only a cross-family review. Unknown fields remain rejected in both versions.

### Configuration API

The Python API adds immutable values equivalent to:

```python
@dataclass(frozen=True)
class SameFamilyReviewException:
    id: str
    registry_version: str
    implementer_family: str
    implementer_models: tuple[str, ...]
    reviewer_model: str
    required_for_implementer_models: bool
    owner_authorization: str
    rationale: str

class ReviewPolicyMode(StrEnum):
    STAGED = "staged"
    ENFORCED = "enforced"

@dataclass(frozen=True)
class ReviewPolicy:
    mode: ReviewPolicyMode | None = None
    owner_authorization: str | None = None
    forbidden_reviewer_families: tuple[str, ...] = ()
    same_family_exceptions: tuple[SameFamilyReviewException, ...] = ()
```

`ProjectConfig.review_policy` is optional and defaults to an empty `ReviewPolicy`. The JSON
shape is:

```json
{
  "review_policy": {
    "mode": "staged",
    "owner_authorization": "Radan; owner instruction 2026-09-05",
    "forbidden_reviewer_families": ["anthropic"],
    "same_family_exceptions": [
      {
        "id": "publyapp-gpt-implementation-sol-review-v1",
        "registry_version": "models-v1",
        "implementer_family": "openai",
        "implementer_models": ["gpt-5.6-luna"],
        "reviewer_model": "gpt-5.6-sol",
        "required_for_implementer_models": true,
        "owner_authorization": "Radan; owner instruction 2026-09-05",
        "rationale": "GPT implementation is reviewed by gpt-5.6-sol; Claude is forbidden."
      }
    ]
  }
}
```

Configuration validation is fail-closed:

- `review_policy` must be an object; its keys are exact.
- An absent policy, or a policy with `mode: null`, `owner_authorization: null`, and both collections
  empty, is disabled. Every non-empty policy requires an explicit `mode` of `staged` or `enforced`
  and a non-blank `owner_authorization`; content cannot activate implicitly.
- Family names are normalized through the existing resolver and must be known.
- Exception IDs are non-blank, unique, and stable path-safe identifiers.
- Model IDs are non-blank raw canonical registry keys with no whitespace; each must resolve to a
  known family. Trimming or case-folding an input is not normalization and cannot make it valid.
- `implementer_models` is non-empty and unique.
- `registry_version` names one released immutable registry, and every `implementer_models` entry
  must map to `implementer_family` in that exact registry version.
- `reviewer_model`, `owner_authorization`, and `rationale` are non-blank.
- `required_for_implementer_models` is an exact boolean. When true, every listed implementer model
  must use this exception ID and exact reviewer; an ordinary cross-family review cannot bypass it.
- The exception's model families must be equal; otherwise it is not a same-family exception.
- `reviewer_model` is exact and cannot also appear in `implementer_models`; the policy cannot turn
  Sol implementation into Sol self-review.
- An exception cannot authorize a reviewer family listed in `forbidden_reviewer_families`.
- Duplicate exceptions or duplicate forbidden families are rejected.
- One implementer model cannot appear in two required routes.
- No policy is inferred from the project name, repository name, environment, or reviewer text.

#### Policy lifecycle and staging

The owner-authorized policy has three effective modes:

- **disabled** — `review_policy` is absent or exactly empty. Existing schema-v1 cross-family
  authority remains unchanged.
- **staged** — the full candidate policy is loaded and strictly validated. `import-review` may
  import schema-v2 records that satisfy that candidate policy, and retirement/recovery operations
  may migrate existing evidence. Forbidden reviewer families already apply to new imports, so a
  staged PublyApp policy cannot import a new Claude review. However, ordinary `status` always
  returns `UNVERIFIED` with reason `review_policy_staged`; no v1, v2, old, or replacement review can
  produce `APPROVED` or `APPROVED_WITH_FOLLOW_UPS` while staging is in progress.
- **enforced** — the same policy becomes authoritative for normal status derivation. Only compliant
  schema-v2 active reviews can contribute review authority.

`staged` is an explicit migration capability, not a weaker approval policy. While it is set, the
only review-state mutations are importing policy-compliant schema-v2 evidence, retiring old
evidence, and resuming an already prepared retirement. All closure transitions that require an
approved review are denied. The mode cannot be inferred from the presence of policy fields, the
project name, or a migration directory.

Before changing `staged` to `enforced`, `check-policy-activation` evaluates the candidate enforced
policy without granting status authority. It uses the same schema-v2 validator, provenance checks,
tip binding, retirement relations, and policy object as normal status and requires: no active-tip
schema-v1 artifact, no incomplete retirement, no forbidden reviewer, and at least one compliant
active schema-v2 review at the exact pushed tip. Its result is only `ELIGIBLE` or a typed refusal;
it can never emit an approval state. The eligibility record binds the tip, current staged-config
digest, and projected enforced-config digest obtained by changing only `mode`. After `ELIGIBLE`, the
config may change only to that exact enforced digest; normal `status` must then independently derive
the result. Any other config or tip change makes the eligibility record stale.

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

MODEL_REGISTRY_GOLDEN_SHA256 = {
    "models-v1": "8b5e8673ab6d56bcc1d7f435b79ba8503f08057ec820901919dcc5052a8ec0c2",
}
```

`MODEL_REGISTRY_V1` above is the complete `models-v1` mapping. Its golden digest is SHA-256 over
the UTF-8 canonical JSON object with keys sorted, no insignificant whitespace, and separators
`,` and `:`. For schema-v2 records and configuration, the raw `model_id` must equal a registry key
byte-for-byte: there is no trim, case-fold, Unicode normalization, punctuation folding, or implicit
alias lookup. This keeps the
serialized identity auditable and prevents two spellings from becoming the same reviewer by
accident. It rejects internal or surrounding whitespace, control characters, path separators,
empty values, unknown IDs, and labels that identify only a family (`gpt`, `openai`, `claude`, or
`anthropic`). Registry entries map to one immutable family.

Aliases exist only in a separately scoped `legacy-v1` migration reader. They are explicit,
one-to-one, versioned mappings used to inventory or migrate old metadata under a staged or enforced
policy;
they are never accepted in schema-v2/configuration and never satisfy a same-family exception. The
migration reader retains both the original spelling and the resulting canonical key in its report.
This reader is separate from the normal policy-empty schema-v1 authority path, which continues to
use the existing family resolver and accepts existing family-only labels such as `deepseek` and
`claude` exactly as before.

The config validator resolves every exception model through this registry. An unknown ID,
ambiguous alias, family mismatch, or legacy declaration that disagrees with the registry is a
configuration error. The registry version is included in the normalized policy and in the review
provenance, so changing a model's family cannot silently validate an old artifact.

Released registries are append-only by version: `models-v1`, its aliases, canonical serialization,
family mappings, and golden digest can never change. Adding a model or correcting a family creates
a complete `models-v2` snapshot and a new golden digest; old artifacts continue to resolve against
`models-v1`. The loader has no `latest` fallback and rejects an unknown or digest-mismatched version.
A golden test asserts the complete v1 mapping and digest byte-for-byte, so editing v1 rather than
adding v2 fails CI. It separately asserts that `MODEL_ALIASES_V1` remains the exact released empty
mapping, so aliases cannot change without a new version even though the registry digest covers only
the canonical model-to-family mapping.

For PublyApp, the exact policy above is the only same-family exception. `gpt-5.6-luna` is currently
the sole GPT model authorized by the adapter for implementation; `gpt-5.6-sol` is reviewer-only,
while Terra and Codex Spark being routable does not make them authorized implementers. The
exception's `implementer_models` must equal the adapter's complete authorized GPT-implementer set
for its pinned registry version, and an adapter-policy agreement check fails closed on either a
missing or extra model. If an already registered GPT model is later authorized for implementation,
the adapter and policy list change together. If its ID is not registered, first append a complete
`models-v2`, then pin the policy and provenance to v2 and update the exhaustive list. No future GPT
model is authorized implicitly. Every authorized GPT implementation routes to the exact reviewer
`gpt-5.6-sol`; Claude is removed from all PublyApp new-review lanes.

The PublyApp exception sets `required_for_implementer_models: true`. Consequently, a Luna artifact
reviewed by DeepSeek, Claude, Terra, or any model other than exact Sol is rejected under the staged
or enforced PublyApp policy even though some of those pairs are cross-family. The generic
cross-family path remains unchanged only for implementers that do not match a required route and
for projects whose policy is disabled.

### Review validation API

Change the validator to:

```python
validate_review(record, *, review_policy: ReviewPolicy | None = None) -> ReviewRecord
```

`None` is normalized to a disabled empty policy. Validation first dispatches by schema version and
policy mode:

1. For schema v1 with an empty policy, run the existing v1 validator unchanged. It validates the
   exact v1 shape, resolves the two `*_family` fields with `families.resolve_family()`, rejects equal
   resolved families, and preserves all existing finding, live-tip, and verdict validation. This is
   the normal import/status path and its accepted cross-family record remains current-tip authority.
2. For schema v1 with a staged or enforced policy, normal import rejects the record before any
   write. Status treats any active-tip v1 artifact as non-authoritative and returns `UNVERIFIED`; it
   cannot skip the artifact in favor of another review. Only the separate migration reader may
   parse it for an inventory or retirement decision. Replacement with schema v2 and explicit
   retirement are required before approval.
3. For schema v2, resolve both raw model IDs through the artifact provenance's declared immutable
   registry version. Schema-v2 records require exact registry keys; family-only labels and legacy
   aliases are invalid. When an exception is requested, its configured registry version must equal
   the artifact provenance registry version.
4. Derive each family from the registry entry. If any supplied legacy family label, explicit family,
   or model field disagrees with that result, reject the record, even when both values happen to be
   in the same lineage. Never accept a same-lineage record merely because its labels agree.
5. Reject a reviewer whose resolved family is forbidden by a staged or enforced project policy.
6. If the implementer model matches an exception with `required_for_implementer_models: true`,
   reject unless `review_exception.policy_id` and `reviewer_model` exactly match that route. This
   check runs before the generic cross-family path.
7. Otherwise, if the resolved families differ, accept the normal cross-family path and reject any
   attached same-family exception declaration.
8. If the resolved families are equal, reject unless a complete `review_exception` is present and
   exactly matches one configured exception ID, implementer model, and reviewer model.
9. Validate both provenance envelopes against the durable source and digest contract below.
10. Preserve all existing finding, live-tip, and verdict validation. In staged mode a valid record
   may be imported for migration, but ordinary status remains `UNVERIFIED` and grants it no authority.

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
- `provenance.registry_version` must name a released immutable registry. For a requested exception,
  it must equal that exception's configured `registry_version`; a record cannot select a newer
  mapping than the policy that authorizes it.
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
- New schema-v2 producers must populate model fields. With a disabled policy, schema-v1 records
  remain authoritative under the existing cross-family rule through the existing resolver. With a
  staged or enforced policy, schema-v1 records are non-authoritative and cannot use an exception or
  regain authority without schema-v2 replacement plus explicit retirement.

The normalized schema-v2 `ReviewRecord` stores `implementer_model`, `reviewer_model`, the resolved
family values, registry version, provenance descriptors, and the optional exception ID. The
existing normalized schema-v1 record remains unchanged on the policy-empty compatibility path. Raw
JSON remains byte-preserved in the durable store; normalization never rewrites evidence. A project
with a staged or enforced `review_policy` cannot use a schema-v1 record as current-tip approval
because it lacks mandatory provenance. A valid schema-v2 record imported during staging becomes
eligible for authority only after the exact candidate policy enters `enforced` mode and normal
status validates it again.

This is honest about what the artifact proves: it records the model IDs claimed by the lane and
binds them to the reviewed tip, but it does not claim cryptographic proof of provider execution.
The lane output or evidence reference must identify the invocation/run that produced the artifact.
An arbitrary text assertion such as `reviewer_family: "openai"` is never sufficient to establish
that `gpt-5.6-sol` actually reviewed the commit.

## Import and status consistency

`cmd_import_review()` passes `config.review_policy` to `validate_review()` before writing any
artifact. The status path passes the same object while parsing every result from
`store.bound_reviews()`. No second policy implementation is permitted.

The version/policy matrix is normative:

| Artifact | Policy | `import-review` | `status` |
|---|---|---|---|
| schema v1 | disabled | Existing v1 validator; write only if cross-family | Existing v1 validator; accepted cross-family review remains authority |
| schema v1 | staged or enforced | Reject before write | `UNVERIFIED` until schema-v2 replacement and retirement |
| schema v2 | disabled | Schema-v2 cross-family validator and provenance checks | Identical validator; same-family remains forbidden |
| schema v2 | staged | Candidate-policy validator permits migration import; forbidden reviewer families already rejected | Always `UNVERIFIED`; staged evidence has no approval authority |
| schema v2 | enforced | Enforced-policy validator and provenance checks | Identical validator; compliant active review may contribute authority |

An empty policy means `mode` and `owner_authorization` are `None` and both collections in
`ReviewPolicy` are empty; it is semantically identical to an absent `review_policy`. A project name,
repository name, schema-v2 support, or staged import cannot implicitly activate approval authority.

The policy is loaded and validated once with `ProjectConfig`; its normalized immutable value is
passed through both paths. A policy change invalidates no bytes and silently upgrades no record.
If a durable record or its provenance no longer satisfies the enforced policy, status returns a typed
malformed/unverified error rather than treating another review or a projection as approval
evidence. Import validates provenance before creating the review event, and status validates the
same envelope, path, registry version, model IDs, reviewed commit, and digest before considering
the review verdict. Staged import calls this same validator but cannot bypass the mode-level
`UNVERIFIED` status result.

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
non-blank and policy-scoped, and the expected digest is mandatory. `requested_at` is assigned only
when PREPARED is first published. A replay loads that immutable envelope and compares all
caller-supplied semantic arguments without generating a new timestamp. It is idempotent only when
those arguments match exactly; a different reason, policy, source, or digest for the same
retirement ID is a hard conflict.

The source, staging copy, and final artifact are three distinct no-replace paths:

```text
reviews/<commit>/<review-id>.json
reviews-retirement-staging/<commit>/<retirement-id>.json
reviews-retired/<commit>/<retirement-id>.json
```

The state machine is `ACTIVE -> PREPARED -> COPIED -> COMMITTED -> FINALIZED`. The operation:

1. reads the active artifact through its descriptor, hashes its immutable bytes, and verifies the
   original `REVIEW_EVENT` binding and caller-supplied digest;
2. atomically publishes and fsyncs the immutable retirement envelope at a unique no-replace path
   beneath `retirements/<commit>/<retirement-id>/`, then appends its
   `REVIEW_RETIREMENT_PREPARED` event. Publication of the envelope produces `PREPARED` and
   immediately revokes the source review's authority, even though the source path still exists;
3. exclusively creates the staging-copy path, copies the source bytes, fsyncs file and parent,
   re-hashes it, then appends `REVIEW_RETIREMENT_COPIED`;
4. appends and fsyncs `REVIEW_RETIREMENT_COMMITTED`, containing all envelope arguments and the
   source, staging, and final paths with their matching digest;
5. atomically moves the original active artifact, not the staging copy, to the previously absent
   final path using a no-replace rename primitive. The staging proof copy remains at its distinct
   durable path. The operation fsyncs both parent directories, verifies source absence plus exact
   staging/final digests, then appends and fsyncs `REVIEW_RETIREMENT_FINALIZED`.

No step overwrites an existing path. If the platform cannot provide an atomic no-replace rename,
retirement refuses to start. A pre-existing staging or final target is accepted only as an
idempotent replay when the same retirement envelope and event sequence already bind its exact path
and digest; otherwise it is a hard conflict.

`bound_reviews()` stops treating the source review as authority as soon as any retirement envelope
has been published at its deterministic `PREPARED` path. A missing PREPARED event, incomplete later
state, or conflicting envelope makes status `UNVERIFIED`; an orphan envelope can never leave the
source active. Every state from `PREPARED` through `COMMITTED` remains `UNVERIFIED`. Only a complete
`FINALIZED` envelope/event/source-absence/staging/final-digest relation lets status exclude the old
review and continue deriving authority from other active reviews.

A recovery pass resumes a valid in-progress envelope from its last durable state, verifies every
path and digest before advancing, and never restores source authority. A crash before publication
of the PREPARED envelope leaves the active review authoritative; a crash at or after publication
leaves the PR `UNVERIFIED` until the same retirement is finalized. There is no ordinary abort or
automatic reactivation path. `validate_event_relations()` validates the complete state machine,
envelope equality, source/staging/final containment, event order, original review binding, source
absence at finalization, and every byte digest.

The move and event append must be crash-safe and idempotent. A missing event, changed digest,
foreign path, duplicate incompatible retirement, or partially moved artifact fails closed. A
Claude artifact uses reason `policy-migration: claude-reviewer-forbidden`; a non-Claude schema-v1
artifact uses `policy-migration: schema-v2-provenance-required`; and rollback of an old same-family
artifact uses `policy-rollback: same-family-review-replaced`. None claims that the historical review
was invalid under the policy that applied when it was created.

Migration of an active tip therefore happens only under an explicitly staged PublyApp policy:

1. stage the exact policy and remove Claude from new-review routing; status becomes intentionally
   `UNVERIFIED`;
2. obtain and import a fresh allowed schema-v2 review at the exact pushed tip, with Sol for every
   policy-authorized GPT implementer;
3. retire every active schema-v1 or forbidden-reviewer artifact through the durable state machine;
4. require `check-policy-activation` to return `ELIGIBLE` at the same tip and candidate config
   digest;
5. change only the mode to `enforced`, then run normal `status` and confirm only compliant active
   schema-v2 reviews contribute to the verdict.

Historical closed PRs need no rewrite. Their old artifacts remain in the retired or historical
namespace and are not eligible as current-tip authority. No migration step may edit JSON bytes in
place, invent a Sol review, or delete evidence.

## Tests

### Contract and schema

- policy absent or explicitly empty preserves the current schema-v1 default, including existing
  family-only labels and cross-family authority;
- valid PublyApp policy parses to normalized immutable values;
- staged and enforced modes are explicit; a non-empty policy without a mode or owner authorization,
  a disabled policy with content, or any unknown mode is rejected;
- the complete `models-v1` registry contains every active adapter model exactly once, and no
  unlisted model ID or family-only label validates;
- the complete canonical `models-v1` serialization hashes to
  `8b5e8673ab6d56bcc1d7f435b79ba8503f08057ec820901919dcc5052a8ec0c2`; changing any v1 key,
  family, or golden digest fails, while additions require a complete `models-v2` snapshot;
- the released `MODEL_ALIASES_V1` mapping is asserted separately as exactly empty and cannot change
  under the v1 name;
- artifacts pinned to models-v1 keep the exact v1 mapping after models-v2 is introduced, with no
  latest-version fallback;
- PublyApp's exception list equals the adapter's full authorized GPT-implementer set for the pinned
  registry and rejects missing or extra models; adding a new GPT implementation lane requires the
  policy/adapter update and, for an unknown ID, a new registry version;
- every PublyApp GPT implementer matches exactly one required route to `gpt-5.6-sol`; a cross-family
  reviewer cannot bypass that required route;
- schema-v2/config accepts only byte-exact lowercase registry keys; surrounding whitespace,
  case variants, Unicode lookalikes, and punctuation variants are rejected;
- legacy aliases are accepted only by the explicitly scoped inventory reader and never by a
  same-family exception;
- blank, duplicate, unknown, or conflicting policy fields are rejected;
- a forbidden reviewer family in an exception is rejected;
- schema/Python agreement covers project policy and the schema-v2 artifact/provenance fields;
- schema-v1 uses the normal existing validator and can remain current-tip cross-family authority
  only when policy is absent or empty;
- schema-v1 is accepted only by the historical/migration reader when policy is staged or enforced
  and cannot become current-tip approval in either mode;
- missing provenance, wrong registry version, unknown run ref, path escape, symlink, missing file,
  changed bytes, or SHA-256 mismatch is rejected by both import and status;
- implementer and reviewer provenance are both mandatory; an artifact with only reviewer output
  cannot import;
- JSON Schema semantic-asymmetry comments name policy lookup, family comparison, and canonical
  model resolution as Python-only checks.

### Review validator

- same family with no policy is rejected;
- schema-v1 `deepseek` to `claude` remains accepted by the existing family resolver when policy is
  absent or empty, while schema-v1 `gpt-4o` to `gpt-5` remains rejected as same-family;
- same family with a policy ID absent from config is rejected;
- every GPT implementer enumerated by the PublyApp policy is accepted only with exact reviewer
  `gpt-5.6-sol` and the configured exception ID;
- a GPT model absent from the exhaustive implementer list, Sol as its own implementer/reviewer, a
  different reviewer, or a different policy ID is rejected;
- a registry alias normalizes only to its one explicit canonical ID; an ambiguous alias is rejected;
- a legacy alias or family-only legacy declaration cannot satisfy the GPT-to-Sol exception;
- same-family with missing model fields or missing exception is rejected;
- cross-family with an exception is rejected;
- Claude reviewer is rejected under the PublyApp policy;
- GPT→DeepSeek and DeepSeek→GPT remain ordinary cross-family approvals under a disabled policy;
  PublyApp's staged or enforced required GPT-to-Sol route rejects GPT→DeepSeek;
- model/family mismatches, unknown model IDs, lookalikes, and case/whitespace tricks are rejected;
- all existing findings, mandatory scopes, duplicate IDs, verdict promotion, and live binding tests
  remain green.

### CLI and state

- import applies the policy before any write;
- status applies the identical policy to durable artifacts;
- staged policy accepts only policy-compliant schema-v2 imports, rejects new Claude reviews, permits
  retirement/recovery, and makes status unconditionally `UNVERIFIED` with no approval authority;
- `check-policy-activation` refuses a v1 artifact, forbidden reviewer, incomplete retirement,
  config/tip mismatch, or missing compliant v2 review; an eligible result still cannot approve;
- staged deactivation keeps Anthropic forbidden, permits a genuine policy-free non-Claude
  cross-family schema-v2 import, and remains `UNVERIFIED` until policy removal;
- enforced status revalidates independently after a staged-to-enforced change that modifies only
  the mode;
- schema-v1 import and status with absent or empty policy preserve the existing cross-family
  authority path and all existing v1 regression tests remain green;
- schema-v1 import with a staged or enforced policy rejects before writing, and status with an
  active-tip v1 artifact returns `UNVERIFIED` even when that artifact was a valid cross-family
  approval before policy activation;
- an accepted same-family artifact cannot be read as approved when the config exception is removed;
- a legacy cross-family artifact remains authoritative only on the policy-empty compatibility path;
- an old Claude artifact plus a new Sol artifact fails until the old artifact is explicitly retired;
- rollout inventory catches every schema-v1 artifact at the active tip, including old
  cross-family records and records with no Claude;
- missing or mutable implementer output forces a fresh implementation verification run, and no
  migration path can manufacture implementer provenance from the review JSON;
- retirement preserves the original digest and event relation;
- retirement envelopes require exact args, reason, policy ID, expected digest, and a unique ID;
- PREPARED envelope publication immediately revokes source authority; every incomplete state is
  `UNVERIFIED`, even while the original source path still exists;
- staging and final targets are distinct no-replace paths, the original active artifact moves to
  the absent final target, and collisions or overwrite-capable fallbacks are rejected;
- crashes injected before and after every retirement state transition recover idempotently, while
  an incomplete state remains `UNVERIFIED`;
- missing, forged, duplicate, or conflicting retirement events fail closed;
- a new commit or reviewed-tip mismatch still invalidates all review authority.

## Rollout and rollback

Rollout is two phases:

1. ship the shared optional policy, validator, generated schemas, retirement primitive, and tests;
2. enter PublyApp's explicit staged-policy workflow, migrate active tips, prove activation
   eligibility, then enforce the policy.

Before staging the PublyApp policy, inventory current active-tip artifacts and record which ones are
schema v1, regardless of whether they mention Claude. The inventory has two required halves for
every artifact at every active PR tip: implementer provenance and reviewer provenance. It records
the original JSON digest, both model declarations, whether each side has an immutable lane output,
and the exact replacement/retirement decision. The inventory is advisory; the gate remains
authoritative.

Then set the exact candidate `review_policy.mode` to `staged` and remove Claude from every
new-review route in the same rollout checkpoint. This is the sole authorized bridge across the
migration circularity: staged mode allows policy-compliant v2 imports and retirement operations,
but status is fail-closed `UNVERIFIED` for the entire window. No Claude review is requested or
imported. A real non-Claude cross-family reviewer remains available for non-GPT implementation;
every authorized GPT implementation uses the exact staged GPT-to-Sol exception.

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
4. Retire the old schema-v1 artifact with the durable retirement protocol. Staged `status` must
   remain `UNVERIFIED`; this is the expected safety result, not an activation failure.

Only after every active-tip schema-v1 artifact has a replacement or an explicitly verified
historical retirement may `check-policy-activation` run. This includes old cross-family records;
the policy rollout is not a Claude-only cleanup. The check must return `ELIGIBLE` at the exact tip
and both the staged and projected enforced config digests. The rollout then changes only
`mode: staged` to `mode: enforced`, producing that exact enforced digest, and requires a fresh normal
status derivation. Failure at either step leaves or restores staged mode; it never falls back to
Claude or treats staged evidence as approval.

Rollback is a verified staged deactivation, not a direct config edit:

1. Replace the enforced policy with an owner-authorized staged deactivation config that retains
   `forbidden_reviewer_families: ["anthropic"]` but has no same-family exception. Status immediately
   becomes `UNVERIFIED`. This explicitly permits migration toward the disabled cross-family default
   without permitting Claude or preserving the required GPT-to-Sol route as import authority.
2. At the exact current pushed tip, obtain a fresh non-Claude, cross-family schema-v2 review with
   complete provenance. For a GPT implementation, use a real registered non-Claude reviewer already
   available to the adapter, such as `deepseek-v4-flash`; do not invent a provider or model.
3. Import that review through the staged deactivation policy and validate its provenance digest,
   local gates, CI, and live-tip binding. It remains non-authoritative while mode is staged.
4. Retire the old same-family review and any forbidden-reviewer artifact through the complete
   `ACTIVE -> PREPARED -> COPIED -> COMMITTED -> FINALIZED` protocol.
5. Run `check-policy-deactivation`. It evaluates the projected disabled empty policy with the same
   validators and requires the new cross-family review to be the sole active review authority, no
   incomplete retirement, exact tip binding, and green non-review gates. It records both staged and
   projected disabled config digests but cannot emit approval.
6. Remove `review_policy` only after that eligibility proof, then run normal `status`; it must derive
   green under the empty default policy from the fresh non-Claude cross-family review.

If any step fails, remain in or restore the staged deactivation config and resume the same bounded
migration; status stays `UNVERIFIED`. Never restore review authority from an incomplete retirement,
delete the replacement, fall back to Claude, or reactivate retired evidence automatically. A
separately audited restore operation would have to verify the original digest and append a new
event; it is not part of ordinary rollback.

## Non-goals

- A global `allow_same_family` switch.
- Treating provider host (`cline-pass`) as a model family.
- Treating a chat/Trello approval or a free-form reviewer name as model provenance.
- Rewriting historical review JSON.
- Automatically merging or approving a PR.
- Relaxing any existing tip, CI, finding, follow-up, or evidence checks.
