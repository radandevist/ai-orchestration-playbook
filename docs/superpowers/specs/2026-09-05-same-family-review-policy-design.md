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
  of the shared rule. The single machine-readable route table enumerates the complete authorized
  GPT implementer set; prose, a duplicate constant, or family inference cannot widen it.
- Every other project and every unconfigured route continues to reject same-family review.

The gate remains fail-closed. A missing policy, malformed policy, unknown model, missing
exception declaration, stale artifact, or contradictory family claim cannot produce approval.

## Current contract and constraints

`validate_review()` currently resolves `implementer_family` and `reviewer_family`, then rejects
equal resolved families. `ProjectConfig` has no review policy or machine route table.
`cmd_import_review()` and the read-only `status` path both validate durable review records, so they
must consume the same normalized policy and route objects.

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
    reviewer_model: str
    required_for_authorized_family: bool
    owner_authorization: str
    rationale: str

@dataclass(frozen=True)
class ModelRoute:
    id: str
    registry_version: str
    launcher_registry_version: str
    implementer_model: str
    implementer_runner: str
    implementer_invocation_model: str
    reviewer_model: str
    reviewer_runner: str
    reviewer_invocation_model: str
    same_family_policy_id: str | None

class ReviewPolicyMode(StrEnum):
    STAGED = "staged"
    ENFORCED = "enforced"

@dataclass(frozen=True)
class ReviewPolicy:
    mode: ReviewPolicyMode | None = None
    owner_authorization: str | None = None
    forbidden_reviewer_families: tuple[str, ...] = ()
    same_family_exceptions: tuple[SameFamilyReviewException, ...] = ()

# Optional for legacy policy-disabled projects; mandatory when review_policy is non-empty.
ProjectConfig.model_routes: tuple[ModelRoute, ...] = ()
```

`ProjectConfig.review_policy` is optional and defaults to an empty `ReviewPolicy`. The JSON
shape is:

```json
{
  "model_routes": [
    {
      "id": "publyapp-luna-to-sol-v1",
      "registry_version": "models-v1",
      "launcher_registry_version": "launchers-v1",
      "implementer_model": "gpt-5.6-luna",
      "implementer_runner": "codex",
      "implementer_invocation_model": "gpt-5.6-luna",
      "reviewer_model": "gpt-5.6-sol",
      "reviewer_runner": "codex",
      "reviewer_invocation_model": "gpt-5.6-sol",
      "same_family_policy_id": "publyapp-gpt-implementation-sol-review-v1"
    },
    {
      "id": "publyapp-deepseek-to-sol-v1",
      "registry_version": "models-v1",
      "launcher_registry_version": "launchers-v1",
      "implementer_model": "deepseek-v4-flash",
      "implementer_runner": "opencode",
      "implementer_invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
      "reviewer_model": "gpt-5.6-sol",
      "reviewer_runner": "codex",
      "reviewer_invocation_model": "gpt-5.6-sol",
      "same_family_policy_id": null
    }
  ],
  "review_policy": {
    "mode": "staged",
    "owner_authorization": "Radan; owner instruction 2026-09-05",
    "forbidden_reviewer_families": ["anthropic"],
    "same_family_exceptions": [
      {
        "id": "publyapp-gpt-implementation-sol-review-v1",
        "registry_version": "models-v1",
        "implementer_family": "openai",
        "reviewer_model": "gpt-5.6-sol",
        "required_for_authorized_family": true,
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
- A non-empty review policy requires a non-empty `model_routes` table. Route IDs and implementer
  models are unique; every route field is exact and non-blank except nullable
  `same_family_policy_id`.
- A disabled policy requires an empty route table, preserving legacy behavior. A route table can
  neither authorize dispatch nor constrain closure without a staged or enforced policy beside it.
- A route's `registry_version` and `launcher_registry_version` name released immutable registries.
  Both canonical model IDs must exist in the exact model-registry version, and each complete
  `(model_id, runner, invocation_model)` endpoint must exist in the exact launcher-registry version.
  These values are passed to dispatch verbatim; no component reconstructs an invocation string.
- `reviewer_model`, `owner_authorization`, and `rationale` are non-blank.
- `required_for_authorized_family` is an exact boolean. When true, every authorized model route
  whose implementer belongs to that family must name this exception ID and exact reviewer; an
  ordinary cross-family reviewer cannot bypass it.
- The exception's model families must be equal; otherwise it is not a same-family exception.
- A route cannot use the same canonical model as implementer and reviewer; the policy cannot turn
  Sol implementation into Sol self-review.
- An exception cannot authorize a reviewer family listed in `forbidden_reviewer_families`.
- Duplicate exceptions or duplicate forbidden families are rejected.
- One implementer model cannot appear in two routes.
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

#### Immutable launcher identity registry

Canonical model identity does not prove that a runner can invoke that model. The shared launcher
layer therefore publishes a second machine-readable registry whose entries are exact endpoint
identities, not project routes or review permissions:

```python
LAUNCHER_REGISTRY_V1 = {
    ("deepseek-v4-flash", "opencode", "cline-pass/cline-pass/deepseek-v4-flash"),
    ("gpt-5.6-luna", "codex", "gpt-5.6-luna"),
    ("gpt-5.6-sol", "codex", "gpt-5.6-sol"),
}

LAUNCHER_REGISTRY_GOLDEN_SHA256 = {
    "launchers-v1": "b7a4dd007921ffe1bf57e7c01a02bec95b9a32013d959500e411b7d9c88416c6",
}
```

This is the complete `launchers-v1` set. Its canonical serialization is a JSON array of
`[model_id, runner, invocation_model]` arrays, sorted lexicographically, with no insignificant
whitespace and separators `,` and `:`. At validation, every endpoint's model ID must independently
exist in the artifact- or route-declared immutable model registry. Released launcher registries are
immutable complete snapshots: changing an endpoint or adding a real launcher capability creates
`launchers-v2` and a new golden digest; there is no `latest` fallback.

Membership means only that the shared launcher recognizes that exact endpoint. It does not
authorize a model for a project, choose a reviewer, or establish that one participant reviewed the
other. Those facts come respectively from an active project's `model_routes`, the review artifact,
and the two immutable run manifests. Import and status require every schema-v2 provenance
participant's complete endpoint identity to match both its durable manifest and one entry in the
artifact's declared launcher-registry version. Unknown runner names, alternate invocation aliases,
and plausible strings are rejected; the validator never synthesizes an endpoint or route.

#### Single machine-readable route authority

For a staged or enforced project policy, `ProjectConfig.model_routes` is the sole authority for
allowed implementer models and their review routes. The dispatcher loads it to select the
implementation runner/model and the required review runner/model. Import, status, and
policy-transition checks load the same normalized objects to validate provenance and reviewer
choice. The adapter Markdown may explain the routes but cannot authorize one, and neither dispatch
nor closure scrapes or interprets Markdown. No second project model list, runner map, or adapter
constant is permitted.

The launcher registry is deliberately not a second project-route source: it describes globally
real endpoints, while `model_routes` grants project-specific dispatch and reviewer choice only in
staged or enforced mode. With policy disabled, schema-v2 validation consults no `model_routes`
entry. It verifies each claimed participant independently against the launcher registry and its
immutable manifest, then applies the generic cross-family rule. It neither authorizes dispatch nor
invents a reviewer pairing that is absent from project configuration.

For PublyApp, the table above authorizes exactly two implementation routes today:

- `gpt-5.6-luna` through the existing Codex runner, reviewed by exact `gpt-5.6-sol` through Codex,
  with the named same-family exception; and
- `deepseek-v4-flash` through the existing `cline-pass` OpenCode invocation, reviewed by exact
  `gpt-5.6-sol` through Codex as an ordinary cross-family review.

The registry only establishes identity and family; a registered or routable model is not an
authorized implementer until it has exactly one `model_routes` entry. The PublyApp exception sets
`required_for_authorized_family: true`, so every authorized route whose implementer resolves to
OpenAI must select exact Sol and name that exception. Adding Terra, Codex Spark, or another GPT
model to `model_routes` without those fields makes config loading, dispatch, import, and status fail
closed. If all required endpoint identities already exist in the pinned model and launcher
registries, only the single route table changes. Otherwise publish the next complete immutable
registry snapshot first, then pin the new route and exception to those versions. No future GPT
model or launcher endpoint is authorized implicitly. Claude is absent from every PublyApp reviewer
route and remains forbidden.

### Review validation API

Change the validator to:

```python
validate_review(
    record,
    *,
    review_policy: ReviewPolicy | None = None,
    model_routes: tuple[ModelRoute, ...] = (),
) -> ReviewRecord
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
5. For every schema-v2 record, require each participant's exact
   `(model_id, runner, invocation_model)` tuple to exist in the declared immutable launcher registry
   and to equal the tuple in that participant's immutable run manifest and provenance envelope.
   Reject an unknown or mismatched tuple; never derive one from the model ID.
6. With a disabled policy and the required empty route table, do not look up or infer a project
   route. Reject `review_exception`, reject equal resolved families, and otherwise accept the
   generic cross-family pairing only after both independent launcher identities and manifests pass.
7. Under a staged or enforced policy, require the implementer model to match exactly one normalized
   `model_routes` entry. Reject a reviewer whose family is forbidden, or whose canonical model,
   runner, invocation model, model-registry version, or launcher-registry version differs from that
   route.
8. If the matched route's families are equal, require `same_family_policy_id` and
   `review_exception.policy_id` to name the same configured exception. The exception registry
   version, implementer family, and reviewer model must match the route exactly.
9. If an exception has `required_for_authorized_family: true`, reject the entire configuration
   unless every route for that implementer family selects the exception's exact reviewer and policy
   ID. This validation happens at config load and again before dispatch/import/status.
10. If the matched route's families differ, require `same_family_policy_id` and
   `review_exception` to be absent; accept it as the configured cross-family path.
11. Validate both provenance envelopes against the durable source and digest contract below. Route
    concordance is an additional check only in staged or enforced mode.
12. Preserve all existing finding, live-tip, and verdict validation. In staged mode a valid record
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
    "launcher_registry_version": "launchers-v1",
    "implementer": {
      "model_id": "gpt-5.6-luna",
      "runner": "codex",
      "invocation_model": "gpt-5.6-luna",
      "run_ref": "orchestration://run/2026-09-05/luna-123",
      "durable_path": "/home/radan/.hermes/orchestration/closure/publyapp/2104/provenance/impl.json",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    },
    "reviewer": {
      "model_id": "gpt-5.6-sol",
      "runner": "codex",
      "invocation_model": "gpt-5.6-sol",
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
  path. Its keys are exact: `registry_version`, `launcher_registry_version`, `implementer`, and
  `reviewer`.
- Each provenance participant has exactly `model_id`, `runner`, `invocation_model`, `run_ref`,
  `durable_path`, and `sha256`. The first three must match one exact entry in
  `launcher_registry_version` and the corresponding immutable run manifest byte-for-byte;
  `model_id` must also equal the artifact model field and resolve through `registry_version`.
  Under staged or enforced policy, the same tuple and both registry versions must additionally
  equal the corresponding `model_routes` endpoint. Under disabled policy there is no route
  concordance check. `run_ref` is a non-blank immutable lane/run identifier that must resolve to a
  durable run-manifest entry under the configured closure root. The manifest must name the same
  repository, PR, reviewed commit, canonical model ID, runner, invocation model, model-registry
  version, launcher-registry version, and output digest. A string that is merely plausible or
  supplied only in the review JSON is not a valid run reference.
- `provenance.registry_version` must name a released immutable registry. For a requested exception,
  it must equal that exception's configured `registry_version`; a record cannot select a newer
  mapping than the policy that authorizes it.
- `provenance.launcher_registry_version` must name a released immutable launcher registry. It must
  equal the route's `launcher_registry_version` only when policy is staged or enforced; disabled
  validation has no project route to match.
- `durable_path` must be absolute, resolve without a symlink escape, be a regular readable file
  under the configured durable closure root, and not live under a temporary session directory.
  The importer reads its bytes and requires the lowercase 64-hex `sha256` to match. Status repeats
  the path, regular-file, root, and digest checks on every read; missing, replaced, or inaccessible
  provenance is `UNVERIFIED`, never an approval.
- The provenance file is an immutable lane envelope containing the run ref, canonical model ID,
  model- and launcher-registry versions, runner, invocation model, reviewed commit, and producer
  output digest. There are two mandatory halves:
  one immutable implementer-lane output and one immutable reviewer-lane output. The referenced run
  manifests and both envelopes are read and digest-checked at import and status. The review JSON
  may reference them, but cannot replace either half with `local_evidence` text.
- For a same-family exception, the model fields are mandatory even when legacy family fields are
  also present. The validator derives the family from these model fields and checks every duplicate
  declaration for consistency.
- New schema-v2 producers must populate model fields, runner and invocation identities, and both
  registry versions. With a disabled policy, schema-v1 records remain authoritative under the
  existing cross-family rule through the existing resolver; schema-v2 records additionally require
  two launcher-registered, manifest-backed identities but no project route. With a staged or
  enforced policy, schema-v1 records are non-authoritative and cannot use an exception or regain
  authority without schema-v2 replacement plus explicit retirement.

The normalized schema-v2 `ReviewRecord` stores `implementer_model`, `reviewer_model`, the resolved
family values, both registry versions, provenance descriptors, and the optional exception ID. The
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

`cmd_import_review()` passes `config.review_policy` and `config.model_routes` to
`validate_review()` before writing any artifact. The status path passes the same normalized objects
while parsing every result from `store.bound_reviews()`. When policy is staged or enforced, policy
dispatch consumes `config.model_routes` directly and has no independent project-model constants.
Policy-disabled projects retain their legacy orchestration dispatch configuration, but closure does
not read that configuration, convert it into a route, or use it to relax the generic cross-family
rule. No second active-policy route implementation is permitted.

The version/policy matrix is normative:

| Artifact | Policy | `import-review` | `status` |
|---|---|---|---|
| schema v1 | disabled | Existing v1 validator; write only if cross-family | Existing v1 validator; accepted cross-family review remains authority |
| schema v1 | staged or enforced | Reject before write | `UNVERIFIED` until schema-v2 replacement and retirement |
| schema v2 | disabled | Cross-family validator; each participant must match its immutable manifest and launcher-registry identity; no project route lookup | Identical validator; no route is inferred and same-family remains forbidden |
| schema v2 | staged | Candidate-policy validator permits migration import; forbidden reviewer families already rejected | Always `UNVERIFIED`; staged evidence has no approval authority |
| schema v2 | enforced | Enforced-policy validator and provenance checks | Identical validator; compliant active review may contribute authority |

An empty policy means `mode` and `owner_authorization` are `None` and both collections in
`ReviewPolicy` are empty; it is semantically identical to an absent `review_policy`. A project name,
repository name, schema-v2 support, or staged import cannot implicitly activate approval authority.

The policy is loaded and validated once with `ProjectConfig`; its normalized immutable value is
passed through both paths. `model_routes` concordance is evaluated only for staged or enforced
policy. The disabled schema-v2 branch requires the configured route table to be empty and validates
only the generic cross-family rule plus the two independently registered, manifest-backed launcher
identities; it cannot synthesize or persist a project route. A policy change invalidates no bytes
and silently upgrades no record. If a durable record or its provenance no longer satisfies the
enforced policy, status returns a typed malformed/unverified error rather than treating another
review or a projection as approval evidence. Import validates provenance before creating the
review event, and status validates the same envelope, path, registry versions, model IDs, runner and
invocation identities, reviewed commit, and digest before considering
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
- the complete canonical `launchers-v1` serialization contains only the three declared endpoint
  identities and hashes to
  `b7a4dd007921ffe1bf57e7c01a02bec95b9a32013d959500e411b7d9c88416c6`; changing an endpoint
  or digest fails, while a real new launcher capability requires a complete `launchers-v2` snapshot;
- every schema-v2 participant's launcher entry names the same canonical model resolved through the
  artifact's model-registry version, and unknown runners or invocation aliases are rejected rather
  than normalized;
- `ProjectConfig.model_routes` is the only authorized implementer/runner/reviewer source consumed by
  dispatch and closure under staged or enforced policy; no Markdown-derived or duplicated project
  model list is consulted;
- every PublyApp GPT implementer in that table matches exactly one required route to
  `gpt-5.6-sol`; adding Terra, Codex Spark, or another OpenAI model without the exact reviewer and
  exception ID makes config loading red before dispatch;
- adding, removing, or changing a route changes the project-config digest and invalidates stale
  activation evidence and review authority;
- schema-v2/config accepts only byte-exact lowercase registry keys; surrounding whitespace,
  case variants, Unicode lookalikes, and punctuation variants are rejected;
- legacy aliases are accepted only by the explicitly scoped inventory reader and never by a
  same-family exception;
- blank, duplicate, unknown, or conflicting policy fields are rejected;
- a forbidden reviewer family in an exception is rejected;
- schema/Python agreement covers project policy and the schema-v2 artifact/provenance fields;
- schema/Python agreement includes `launcher_registry_version` in routes, artifacts, provenance
  envelopes, and immutable manifests;
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
- disabled schema-v2 accepts a cross-family pair only when each participant independently matches
  an exact launcher-registry entry and immutable manifest; it neither consults `model_routes` nor
  constructs a project route from those two identities;
- disabled schema-v2 rejects a missing launcher version, unknown runner, invocation alias, manifest
  mismatch, or any non-empty route table before writing and again during status;
- schema-v1 `deepseek` to `claude` remains accepted by the existing family resolver when policy is
  absent or empty, while schema-v1 `gpt-4o` to `gpt-5` remains rejected as same-family;
- same family with a policy ID absent from config is rejected;
- every GPT implementer authorized by the PublyApp route table is accepted only with exact reviewer
  `gpt-5.6-sol` and the configured exception ID;
- a GPT model absent from the route table, Sol as its own implementer/reviewer, a
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
- import and status apply launcher-registry and immutable-manifest identity checks to every
  schema-v2 record, while route concordance runs only for staged or enforced policy;
- staged policy accepts only policy-compliant schema-v2 imports, rejects new Claude reviews, permits
  retirement/recovery, and makes status unconditionally `UNVERIFIED` with no approval authority;
- `check-policy-activation` refuses a v1 artifact, forbidden reviewer, incomplete retirement,
  config/tip mismatch, or missing compliant v2 review; an eligible result still cannot approve;
- staged rollback keeps Anthropic forbidden, permits only the target non-Claude cross-family route,
  and remains `UNVERIFIED` until the target policy is enforced;
- rollback activation ends with an enforced policy whose forbidden-family list still contains
  Anthropic and whose same-family exception list is empty; policy removal is not a valid rollback;
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
   durable run manifest, exact reviewed commit, canonical model ID, exact runner and invocation
   model, pinned model- and launcher-registry versions, and independently verifiable digest. Every
   endpoint tuple must exist in that launcher-registry snapshot. A chat transcript, model-family
   label, mutable worktree file, or review JSON alone is not an implementer output.
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

Rollback is a verified transition to a different enforced policy, never policy removal. The target
keeps owner authorization and `forbidden_reviewer_families: ["anthropic"]`, but has
`same_family_exceptions: []`. Its sole `model_routes` table changes the GPT implementer route to the
registered cross-family reviewer `deepseek-v4-flash`, runner `opencode`, invocation model
`cline-pass/cline-pass/deepseek-v4-flash`, pinned to `models-v1` and `launchers-v1`, and
`same_family_policy_id: null`. The DeepSeek implementer route continues to use exact
`gpt-5.6-sol` through `codex` with the same pinned registry versions. Thus the final state cannot
silently recover either Claude authority or the GPT-to-Sol exception.

1. Replace the enforced policy and route table with an owner-authorized `mode: staged` form of that
   exact rollback target. Status immediately becomes `UNVERIFIED`; the staged and projected
   enforced digests are fixed before migration starts.
2. At the exact current pushed tip, obtain a fresh cross-family schema-v2 review for every affected
   GPT implementation from the route table's exact DeepSeek endpoint. Do not invent a provider,
   model, or alternate route.
3. Import each review through the staged target policy and validate its route-bound provenance
   digest, local gates, CI, and live-tip binding. It remains non-authoritative while mode is staged.
4. Retire the old same-family review and any forbidden-reviewer artifact through the complete
   `ACTIVE -> PREPARED -> COPIED -> COMMITTED -> FINALIZED` protocol.
5. Run `check-policy-activation` against the projected enforced rollback target. It uses the same
   route, policy, and closure validators and requires the new cross-family reviews to be the sole
   active review authority, no incomplete retirement, exact tip binding, and green non-review gates.
   It records both staged and projected enforced config digests and can return only `ELIGIBLE`, never
   approval.
6. Change only `mode: staged` to `mode: enforced`, producing the proved target digest, then run normal
   `status`; it must independently derive green under the enforced policy that still forbids
   Anthropic and contains no same-family exception.

If any step fails, remain in or restore the staged form of the rollback target and resume the same
bounded migration; status stays `UNVERIFIED`. Never remove the policy, restore review authority from
an incomplete retirement, delete the replacement, fall back to Claude, or reactivate retired
evidence automatically. A separately audited restore operation would have to verify the original
digest and append a new event; it is not part of ordinary rollback.

## Non-goals

- A global `allow_same_family` switch.
- Treating provider host (`cline-pass`) as a model family.
- Treating a chat/Trello approval or a free-form reviewer name as model provenance.
- Rewriting historical review JSON.
- Automatically merging or approving a PR.
- Relaxing any existing tip, CI, finding, follow-up, or evidence checks.
