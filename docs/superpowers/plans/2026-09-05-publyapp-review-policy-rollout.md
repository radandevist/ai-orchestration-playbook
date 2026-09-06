# PublyApp Review Policy Rollout Plan

> **Dependency:** Execute only after the shared same-family policy tool is implemented, reviewed, and available to PublyApp.

**Goal:** Move PublyApp from legacy family-only review records to exact schema-v2 provenance, authorize only the owner-approved GPT/Luna→GPT-5.6-Sol exception, and keep Anthropic forbidden.

**Architecture:** Rollout is fail-closed. A staged config makes every PR `UNVERIFIED` while active-tip artifacts are inventoried, replaced, and retired. Enforcement begins only after the activation checker proves the exact projected config and tip eligible.

---

### Task 1: Inventory active-tip review authority

- [ ] Enumerate every open PR's active-tip review artifact, including all schema-v1 records rather than only Claude records.
- [ ] For each artifact, record its digest, declared models, exact immutable implementer output, exact immutable reviewer output, and replace/retire decision in a non-secret durable rollout record.
- [ ] Treat missing or mutable provenance as requiring a fresh real run; never manufacture provenance from the review JSON or chat transcript.

### Task 2: Stage the exact owner-authorized policy

- [ ] Add the exact canonical model routes from the approved design to `.ai/project-closure-v1.json` as the only machine-readable dispatch/closure authority.
- [ ] Set `review_policy.mode` to `staged`, include the owner authorization and GPT→Sol exception, and forbid the Anthropic reviewer family.
- [ ] Remove Claude from every new-review route in the same checkpoint.
- [ ] Verify config parsing and confirm status is deliberately `UNVERIFIED` for all affected PRs.

### Task 3: Replace and retire legacy active artifacts

- [ ] Produce fresh immutable implementer or reviewer outputs wherever either verified half is missing.
- [ ] Import policy-compliant schema-v2 reviews at each exact pushed tip.
- [ ] Retire every active schema-v1 or forbidden-reviewer artifact through the durable retirement state machine.
- [ ] Confirm no incomplete retirement and no old artifact retains authority.

### Task 4: Prove activation and enforce

- [ ] Run `check-policy-activation` at every affected exact tip and require `ELIGIBLE` for the staged and projected-enforced config digests.
- [ ] Change only `mode: staged` to `mode: enforced`.
- [ ] Run normal status again and require independent compliant approval authority.
- [ ] Run PublyApp's policy-focused tests, `git diff --check`, and the required local gate before exact-head review.

### Task 5: Prepare the verified rollback target

- [ ] Encode the staged rollback target from the design: Anthropic remains forbidden, same-family exceptions are empty, GPT routes to the registered DeepSeek endpoint, and DeepSeek routes to exact Sol.
- [ ] Test that removing the policy, restoring Claude, or reviving retired evidence can never serve as rollback.
- [ ] Document the staged migration/activation sequence; do not execute rollback unless the owner requests it.

