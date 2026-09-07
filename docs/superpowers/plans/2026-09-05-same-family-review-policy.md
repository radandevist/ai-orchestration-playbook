# Same-Family Review Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and follow the approved design exactly. Keep each task test-first and fail closed.

**Goal:** Replace the unconditional family mismatch check with an explicit, owner-authorized, exact-model review policy that permits PublyApp's GPT/Luna implementations to be reviewed by GPT-5.6 Sol without weakening any unrelated review route.

**Architecture:** The immutable versioned repository-policy registry is the minimum route/policy authority for registered repositories; local configuration may not weaken it. Immutable model and launcher registries resolve byte-exact identities. Schema-v2 review records bind both participants to immutable provenance. Staged mode permits migration but never approval; enforced mode grants authority only to fully compliant active artifacts. Unregistered projects with no policy keep their current schema-v1 behavior.

**Tech Stack:** Python standard library, JSON Schema 2020-12, `unittest`, existing `tools/pr_closure` CLI and immutable store.

---

### Task 1: Add immutable model and launcher registries

**Files:**
- Create: `tools/pr_closure/registries.py`
- Create: `tools/tests/test_registries.py`

- [x] Write RED tests for the complete `models-v1` and `launchers-v1` snapshots, their approved SHA-256 digests, the exactly empty v1 alias table, byte-exact lowercase lookup, and version-pinned lookup with no latest-version fallback.
- [x] Add the minimal immutable registry dataclasses and lookup functions. Include only the model IDs and launcher endpoint tuples defined in the approved design.
- [x] Verify unknown versions, unknown models, family-only labels, case/whitespace variants, Unicode lookalikes, unknown runners, and invocation aliases all fail.
- [x] Run `python3 -m unittest tools.tests.test_registries -v` and confirm GREEN.

### Task 2: Parse the optional project policy and route table

**Files:**
- Modify: `tools/pr_closure/model.py`
- Modify: `tools/pr_closure/contract.py`
- Modify: `tools/schemas/project-closure-v1.json`
- Modify: `tools/tests/test_schema_agreement.py`
- Modify: `tools/tests/test_review.py`

- [x] Write RED contract tests for absent/empty compatibility, exact staged/enforced modes, mandatory owner authorization, exact keys, forbidden reviewer families, same-family exception IDs, and non-empty `model_routes` for active policy.
- [x] Write RED tests proving every route resolves through exact pinned model and launcher registries, route uniqueness holds, and PublyApp GPT routes require exact reviewer `gpt-5.6-sol` plus the approved exception ID.
- [x] Add immutable `ReviewPolicy`, `SameFamilyReviewException`, and `ModelRoute` values to `ProjectConfig`; implement strict parsing without truthy defaults or implicit activation.
- [x] Regenerate the project schema from `project_json_schema()` and extend schema/Python coverage, documenting only genuine Python-only semantic asymmetries.
- [x] Run `python3 -m unittest tools.tests.test_schema_agreement tools.tests.test_policy_config tools.tests.test_registries -v` and confirm GREEN.

### Task 3: Introduce schema-v2 exact identities and provenance

**Files:**
- Modify: `tools/pr_closure/model.py`
- Modify: `tools/pr_closure/contract.py`
- Modify: `tools/pr_closure/review.py`
- Create: `tools/pr_closure/provenance.py`
- Create: `tools/schemas/review-record-v2.json`
- Create: `tools/tests/test_provenance.py`
- Modify: `tools/tests/test_review.py`
- Modify: `tools/tests/test_schema_agreement.py`

- [ ] Write RED tests for the approved schema-v2 participant fields, registry versions, exact canonical models, exact runner/invocation identity, both mandatory provenance envelopes, and immutable manifest digests.
- [ ] Write adversarial RED tests for a missing side, path escape, symlink, missing file, mutable bytes, digest mismatch, unknown run reference, wrong tip, wrong registry version, model/family mismatch, and launcher mismatch.
- [ ] Implement secure no-follow durable-path reads and independent implementer/reviewer provenance validation against immutable manifests and registry snapshots.
- [ ] Preserve schema-v1 parsing exactly on the policy-empty compatibility path; never upgrade a v1 artifact by inference.
- [ ] Generate `review-record-v2.json`, extend agreement coverage, and run the focused review/provenance/schema suites GREEN.

### Task 4: Enforce the policy in one review-authority function

**Files:**
- Modify: `tools/pr_closure/review.py`
- Modify: `tools/tests/test_review.py`

- [ ] Write RED tests for all policy decisions: no-policy same-family rejection; exact allowed GPT→Sol exception; missing/wrong exception rejection; unknown GPT route rejection; Sol→Sol rejection; cross-family-with-exception rejection; forbidden Anthropic reviewer rejection; and required-route rejection of GPT→DeepSeek under staged/enforced PublyApp policy.
- [ ] Refactor `validate_review` so schema parsing, registry/provenance validation, and policy authorization are explicit phases used by both import and status.
- [ ] Ensure policy-disabled schema-v2 only validates the two participant identities independently and never synthesizes a project route.
- [ ] Keep finding promotion, duplicate finding IDs, verdict derivation, and live-tip binding unchanged.
- [ ] Run `python3 -m unittest tools.tests.test_review -v` and confirm GREEN.

### Task 5: Make import and status apply identical authority

**Files:**
- Modify: `tools/pr_closure/cli.py`
- Modify: `tools/pr_closure/store.py`
- Modify: `tools/tests/test_cli.py`
- Modify: `tools/tests/test_store.py`

- [ ] Write RED tests proving `import-review` rejects before any write and `status` independently revalidates the same bytes under the current config.
- [ ] Make active policy reject schema-v1 imports; make staged status unconditionally `UNVERIFIED`; make enforced status ignore no policy violation.
- [ ] Prove removing/changing a route or exception invalidates previously accepted current-tip authority, while an absent/empty policy preserves legacy v1 cross-family authority.
- [ ] Run `python3 -m unittest tools.tests.test_cli tools.tests.test_store -v` and confirm GREEN.

### Task 6: Add durable, crash-safe review retirement

**Files:**
- Modify: `tools/pr_closure/store.py`
- Modify: `tools/pr_closure/cli.py`
- Create: `tools/tests/test_review_retirement.py`
- Modify: `tools/tests/test_store.py`
- Modify: `tools/tests/test_cli.py`

- [ ] Write RED state-machine tests for `ACTIVE -> PREPARED -> COPIED -> COMMITTED -> FINALIZED`, exact source digest binding, unique event IDs, no-replace staging/final targets, and immediate authority revocation at PREPARED.
- [ ] Add crash injection before and after every transition; prove recovery is idempotent and every incomplete or conflicting state is `UNVERIFIED`.
- [ ] Implement retirement using durable event envelopes and immutable moves without overwrite-capable fallbacks or deletion of evidence.
- [ ] Add the exact CLI inputs and allowed retirement reasons from the design; reject missing, forged, duplicate, conflicting, or path-escaping events.
- [ ] Run the retirement, store, and CLI suites GREEN.

### Task 7: Add the staged activation gate

**Files:**
- Modify: `tools/pr_closure/cli.py`
- Modify: `tools/pr_closure/state.py`
- Modify: `tools/tests/test_cli.py`
- Modify: `tools/tests/test_state.py`

- [ ] Write RED tests for `check-policy-activation`: v1 artifact, forbidden reviewer, incomplete retirement, missing compliant v2 artifact, config/tip mismatch, stale route digest, or red non-review gate must be ineligible.
- [ ] Implement an `ELIGIBLE`-only readiness result bound to the exact tip plus staged and projected-enforced config digests; it must never produce approval.
- [ ] Prove the enforced mode independently revalidates after a mode-only staged→enforced change.
- [ ] Pin rollback to a different staged/enforced policy that still forbids Anthropic and contains no same-family exception; policy removal must not qualify.
- [ ] Run CLI/state tests GREEN.

### Task 8: Full shared-tool verification and documentation

**Files:**
- Modify: user-facing closure documentation referenced by the existing CLI
- Verify: `tools/schemas/project-closure-v1.json`
- Verify: `tools/schemas/review-record-v1.json`
- Verify: `tools/schemas/review-record-v2.json`

- [ ] Document configuration, staged rollout, exact-model provenance, retirement recovery, activation, and rollback without presenting a global bypass.
- [ ] Run `python3 -m unittest discover -s tools/tests -v`.
- [ ] Regenerate/check schemas with the repository's existing schema command and confirm no hand-edited drift.
- [ ] Run `git diff --check` and the repository's normal formatting/lint command if present.
- [ ] Review the complete diff against every acceptance item in the approved design; leave no placeholder, compatibility shortcut, or duplicated route authority.

### Task 9: Integrate only after exact-head review

- [ ] Commit the shared tool in small logical commits only after each corresponding suite is green.
- [ ] Obtain an exact-head review from a model family different from the implementer for this architecture/security-sensitive gate.
- [ ] Address findings test-first and repeat the full suite.
- [ ] Do not push or merge until explicitly authorized and until the PublyApp staged rollout plan is ready.
