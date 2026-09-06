import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from pr_closure.store import (
    COMMIT_EVENT,
    REVIEW_EVENT,
    RETIREMENT_COMMITTED_EVENT,
    RETIREMENT_COPIED_EVENT,
    RETIREMENT_FINALIZED_EVENT,
    RETIREMENT_PREPARED_EVENT,
    EvidenceConflict,
    MalformedEvidence,
    RunStore,
)


COMMIT = "a" * 40


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class ReviewRetirementTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="review-retirement-", dir="/var/tmp"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.store = RunStore(self.root, "project", 7)
        self.tip = self.root / "tip.json"
        self.tip.write_text("tip")
        self.store.record_commit(COMMIT, str(self.tip))
        self.record = {
            "schema_version": 1,
            "repository": "owner/repo",
            "pr_number": 7,
            "reviewed_commit": COMMIT,
            "implementer_family": "deepseek",
            "reviewer_family": "openai",
            "verdict": "APPROVED",
        }
        self.store.write_review(COMMIT, "legacy", self.record)
        self.source = self.store.review_path(COMMIT, "legacy")
        self.source_digest = digest(self.source.read_bytes())

    def test_retirement_reaches_finalized_without_overwriting_and_removes_authority(self):
        result = self.store.retire_review(
            repository="owner/repo",
            commit=COMMIT,
            review_id="legacy",
            retirement_id="retire-1",
            reason="policy-migration: schema-v2-provenance-required",
            policy_id="policy-v1",
            expected_sha256=self.source_digest,
        )

        self.assertEqual("FINALIZED", result["state"])
        self.assertFalse(self.source.exists())
        self.assertEqual(
            self.source_digest,
            digest(self.store.retirement_staging_path(COMMIT, "retire-1").read_bytes()),
        )
        self.assertEqual(
            self.source_digest,
            digest(self.store.retirement_final_path(COMMIT, "retire-1").read_bytes()),
        )
        self.assertEqual((), self.store.bound_reviews(COMMIT))
        event_types = [event["event_type"] for event in self.store.read_events()]
        self.assertEqual(
            [
                COMMIT_EVENT,
                REVIEW_EVENT,
                RETIREMENT_PREPARED_EVENT,
                RETIREMENT_COPIED_EVENT,
                RETIREMENT_COMMITTED_EVENT,
                RETIREMENT_FINALIZED_EVENT,
            ],
            event_types,
        )

    def test_concurrent_identical_retirements_are_exactly_once_and_replayable(self):
        retirement_id = "retire-concurrent"
        barrier = threading.Barrier(2)
        original_transition = self.store._append_retirement_transition

        def synchronize_prepared(event_type, *args, **kwargs):
            if event_type == RETIREMENT_PREPARED_EVENT:
                try:
                    barrier.wait(timeout=0.1)
                except threading.BrokenBarrierError:
                    pass
            return original_transition(event_type, *args, **kwargs)

        retirement_args = {
            "repository": "owner/repo",
            "commit": COMMIT,
            "review_id": "legacy",
            "retirement_id": retirement_id,
            "reason": "policy-migration: schema-v2-provenance-required",
            "policy_id": "policy-v1",
            "expected_sha256": self.source_digest,
        }
        with mock.patch.object(
            self.store,
            "_append_retirement_transition",
            side_effect=synchronize_prepared,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(self.store.retire_review, **retirement_args)
                    for _ in range(2)
                ]
                results = [future.result(timeout=10) for future in futures]

        self.assertEqual(["FINALIZED", "FINALIZED"], [result["state"] for result in results])
        self.assertEqual(results[0], results[1])
        self.assertFalse(self.source.exists())
        self.assertEqual(
            self.source_digest,
            digest(self.store.retirement_final_path(COMMIT, retirement_id).read_bytes()),
        )
        self.assertEqual((), self.store.bound_reviews(COMMIT))
        self.assertEqual(
            [
                COMMIT_EVENT,
                REVIEW_EVENT,
                RETIREMENT_PREPARED_EVENT,
                RETIREMENT_COPIED_EVENT,
                RETIREMENT_COMMITTED_EVENT,
                RETIREMENT_FINALIZED_EVENT,
            ],
            [event["event_type"] for event in self.store.read_events()],
        )
        self.assertEqual(
            results[0], self.store.retire_review(**retirement_args)
        )

    def test_prepared_retirement_revokes_authority_and_recovery_is_idempotent(self):
        original_append = self.store.append_event

        def stop_after_prepared(event_type, *args, **kwargs):
            result = original_append(event_type, *args, **kwargs)
            if event_type == RETIREMENT_PREPARED_EVENT:
                raise RuntimeError("crash")
            return result

        self.store.append_event = stop_after_prepared
        with self.assertRaises(RuntimeError):
            self.store.retire_review(
                repository="owner/repo",
                commit=COMMIT,
                review_id="legacy",
                retirement_id="retire-2",
                reason="policy-migration: schema-v2-provenance-required",
                policy_id="policy-v1",
                expected_sha256=self.source_digest,
            )
        self.store.append_event = original_append
        with self.assertRaises(MalformedEvidence):
            self.store.bound_reviews(COMMIT)
        result = self.store.recover_retirement("retire-2", COMMIT)
        self.assertEqual("FINALIZED", result["state"])
        self.assertEqual(result, self.store.recover_retirement("retire-2", COMMIT))

    def test_crash_after_committed_event_recovers_without_overwrite(self):
        original_append = self.store.append_event

        def stop_after_committed(event_type, *args, **kwargs):
            result = original_append(event_type, *args, **kwargs)
            if event_type == RETIREMENT_COMMITTED_EVENT:
                raise RuntimeError("crash")
            return result

        self.store.append_event = stop_after_committed
        with self.assertRaises(RuntimeError):
            self.store.retire_review(
                repository="owner/repo",
                commit=COMMIT,
                review_id="legacy",
                retirement_id="retire-committed",
                reason="policy-migration: schema-v2-provenance-required",
                policy_id="policy-v1",
                expected_sha256=self.source_digest,
            )
        self.store.append_event = original_append
        with self.assertRaises(MalformedEvidence):
            self.store.bound_reviews(COMMIT)
        result = self.store.recover_retirement("retire-committed", COMMIT)
        self.assertEqual("FINALIZED", result["state"])

    def test_crash_after_final_publication_before_finalized_event_recovers(self):
        original_transition = self.store._append_retirement_transition

        def crash_before_finalized(event_type, *args, **kwargs):
            if event_type == RETIREMENT_FINALIZED_EVENT:
                raise RuntimeError("crash after final publication")
            return original_transition(event_type, *args, **kwargs)

        retirement_id = "retire-final-publication"
        with mock.patch.object(
            self.store,
            "_append_retirement_transition",
            side_effect=crash_before_finalized,
        ):
            with self.assertRaises(RuntimeError):
                self.store.retire_review(
                    repository="owner/repo",
                    commit=COMMIT,
                    review_id="legacy",
                    retirement_id=retirement_id,
                    reason="policy-migration: schema-v2-provenance-required",
                    policy_id="policy-v1",
                    expected_sha256=self.source_digest,
                )
        self.assertFalse(self.source.exists())
        self.assertTrue(self.store.retirement_final_path(COMMIT, retirement_id).exists())
        result = self.store.recover_retirement(retirement_id, COMMIT)
        self.assertEqual("FINALIZED", result["state"])

    def test_conflicting_replay_is_rejected(self):
        self.store.retire_review(
            repository="owner/repo",
            commit=COMMIT,
            review_id="legacy",
            retirement_id="retire-3",
            reason="policy-migration: schema-v2-provenance-required",
            policy_id="policy-v1",
            expected_sha256=self.source_digest,
        )
        with self.assertRaises(EvidenceConflict):
            self.store.retire_review(
                repository="owner/repo",
                commit=COMMIT,
                review_id="legacy",
                retirement_id="retire-3",
                reason="policy-migration: claude-reviewer-forbidden",
                policy_id="policy-v1",
                expected_sha256=self.source_digest,
            )

    def test_different_argument_envelope_winner_is_rejected_before_any_retirement_mutation(self):
        rival_id = "legacy-rival"
        self.store.write_review(COMMIT, rival_id, self.record)
        rival_source = self.store.review_path(COMMIT, rival_id)
        envelope_path = self.store.retirement_envelope_path(COMMIT, "retire-race")
        original_atomic_create = self.store._atomic_create
        raced = False

        def publish_rival_winner(target, line):
            nonlocal raced
            if target == envelope_path and not raced:
                raced = True
                candidate = json.loads(line.decode("utf-8"))
                candidate["review_id"] = rival_id
                candidate["source_path"] = str(rival_source.resolve())
                original_atomic_create(envelope_path, json.dumps(candidate, sort_keys=True).encode("utf-8"))
                return False
            return original_atomic_create(target, line)

        with mock.patch.object(
            self.store, "_atomic_create", side_effect=publish_rival_winner
        ):
            with self.assertRaises(EvidenceConflict):
                self.store.retire_review(
                    repository="owner/repo",
                    commit=COMMIT,
                    review_id="legacy",
                    retirement_id="retire-race",
                    reason="policy-migration: schema-v2-provenance-required",
                    policy_id="policy-v1",
                    expected_sha256=self.source_digest,
                )

        self.assertTrue(self.source.exists())
        self.assertTrue(rival_source.exists())
        self.assertFalse(self.store.retirement_staging_path(COMMIT, "retire-race").exists())
        self.assertFalse(self.store.retirement_final_path(COMMIT, "retire-race").exists())
        self.assertEqual(
            [COMMIT_EVENT, REVIEW_EVENT, REVIEW_EVENT],
            [event["event_type"] for event in self.store.read_events()],
        )

    def test_publication_winner_controls_reject_every_immutable_argument_difference(self):
        variants = (
            {"expected_sha256": "0" * 64},
            {"review_id": "legacy-rival"},
            {"retirement_id": "retire-other-target"},
            {"reason": "policy-migration: claude-reviewer-forbidden"},
            {"policy_id": "policy-other"},
            {"repository": "other/repo"},
            {"project": "other-project"},
            {"pr_number": 8},
            {"commit": "b" * 40},
        )
        for index, overrides in enumerate(variants):
            root = Path(tempfile.mkdtemp(prefix="review-retirement-race-", dir="/var/tmp"))
            self.addCleanup(lambda root=root: shutil.rmtree(root, ignore_errors=True))
            store = RunStore(root, "project", 7)
            tip = root / "tip.json"
            tip.write_text("tip")
            store.record_commit(COMMIT, str(tip))
            store.write_review(COMMIT, "legacy", self.record)
            source = store.review_path(COMMIT, "legacy")
            source_digest = digest(source.read_bytes())
            rival_id = "legacy-rival"
            store.write_review(COMMIT, rival_id, self.record)
            rival_source = store.review_path(COMMIT, rival_id)
            original_atomic_create = store._atomic_create
            retirement_id = "retire-race-{0}".format(index)
            envelope_path = store.retirement_envelope_path(COMMIT, retirement_id)

            def publish_variant(target, line, *, overrides=overrides):
                if target == envelope_path:
                    candidate = json.loads(line.decode("utf-8"))
                    candidate.update(overrides)
                    if candidate.get("review_id") == rival_id:
                        candidate["source_path"] = str(rival_source.resolve())
                    if (
                        candidate.get("commit") != COMMIT
                        or candidate.get("pr_number") != 7
                        or candidate.get("project") != "project"
                    ):
                        commit = candidate["commit"]
                        project = candidate["project"]
                        pr = candidate["pr_number"]
                        candidate["source_path"] = str(
                            (
                                root
                                / project
                                / str(pr)
                                / "reviews"
                                / commit
                                / (candidate["review_id"] + ".json")
                            ).resolve()
                        )
                    original_atomic_create(
                        envelope_path,
                        json.dumps(candidate, sort_keys=True).encode("utf-8"),
                    )
                    return False
                return original_atomic_create(target, line)

            with self.subTest(overrides=overrides):
                with mock.patch.object(
                    store, "_atomic_create", side_effect=publish_variant
                ):
                    with self.assertRaises(EvidenceConflict):
                        store.retire_review(
                            repository="owner/repo",
                            commit=COMMIT,
                            review_id="legacy",
                            retirement_id=retirement_id,
                            reason="policy-migration: schema-v2-provenance-required",
                            policy_id="policy-v1",
                            expected_sha256=source_digest,
                        )
                self.assertTrue(source.exists())
                self.assertTrue(rival_source.exists())
                self.assertFalse(
                    store.retirement_staging_path(COMMIT, retirement_id).exists()
                )
                self.assertFalse(
                    store.retirement_final_path(COMMIT, retirement_id).exists()
                )

    def test_recovery_finishes_when_envelope_was_published_before_prepared_event(self):
        original_transition = self.store._append_retirement_transition

        def crash_before_prepared(event_type, *args, **kwargs):
            if event_type == RETIREMENT_PREPARED_EVENT:
                raise RuntimeError("crash before prepared event")
            return original_transition(event_type, *args, **kwargs)

        with mock.patch.object(self.store, "_append_retirement_transition", side_effect=crash_before_prepared):
            with self.assertRaises(RuntimeError):
                self.store.retire_review(
                    repository="owner/repo",
                    commit=COMMIT,
                    review_id="legacy",
                    retirement_id="retire-envelope-crash",
                    reason="policy-migration: schema-v2-provenance-required",
                    policy_id="policy-v1",
                    expected_sha256=self.source_digest,
                )
        result = self.store.recover_retirement("retire-envelope-crash", COMMIT)
        self.assertEqual("FINALIZED", result["state"])

    def test_recovery_finishes_when_staging_was_published_before_copied_event(self):
        original_transition = self.store._append_retirement_transition

        def crash_before_copied(event_type, *args, **kwargs):
            if event_type == RETIREMENT_COPIED_EVENT:
                raise RuntimeError("crash before copied event")
            return original_transition(event_type, *args, **kwargs)

        with mock.patch.object(self.store, "_append_retirement_transition", side_effect=crash_before_copied):
            with self.assertRaises(RuntimeError):
                self.store.retire_review(
                    repository="owner/repo",
                    commit=COMMIT,
                    review_id="legacy",
                    retirement_id="retire-staging-crash",
                    reason="policy-migration: schema-v2-provenance-required",
                    policy_id="policy-v1",
                    expected_sha256=self.source_digest,
                )
        result = self.store.recover_retirement("retire-staging-crash", COMMIT)
        self.assertEqual("FINALIZED", result["state"])

    def test_same_argument_replay_recovers_envelope_without_prepared_event(self):
        original_transition = self.store._append_retirement_transition
        retirement_id = "retire-public-envelope-gap"

        def crash_before_prepared(event_type, *args, **kwargs):
            if event_type == RETIREMENT_PREPARED_EVENT:
                raise RuntimeError("crash before prepared event")
            return original_transition(event_type, *args, **kwargs)

        with mock.patch.object(
            self.store, "_append_retirement_transition", side_effect=crash_before_prepared
        ):
            with self.assertRaises(RuntimeError):
                self.store.retire_review(
                    repository="owner/repo",
                    commit=COMMIT,
                    review_id="legacy",
                    retirement_id=retirement_id,
                    reason="policy-migration: schema-v2-provenance-required",
                    policy_id="policy-v1",
                    expected_sha256=self.source_digest,
                )

        result = self.store.retire_review(
            repository="owner/repo",
            commit=COMMIT,
            review_id="legacy",
            retirement_id=retirement_id,
            reason="policy-migration: schema-v2-provenance-required",
            policy_id="policy-v1",
            expected_sha256=self.source_digest,
        )
        self.assertEqual("FINALIZED", result["state"])

    def test_same_argument_replay_adopts_verified_staging_without_copied_event(self):
        original_transition = self.store._append_retirement_transition
        retirement_id = "retire-public-staging-gap"

        def crash_before_copied(event_type, *args, **kwargs):
            if event_type == RETIREMENT_COPIED_EVENT:
                raise RuntimeError("crash before copied event")
            return original_transition(event_type, *args, **kwargs)

        with mock.patch.object(
            self.store, "_append_retirement_transition", side_effect=crash_before_copied
        ):
            with self.assertRaises(RuntimeError):
                self.store.retire_review(
                    repository="owner/repo",
                    commit=COMMIT,
                    review_id="legacy",
                    retirement_id=retirement_id,
                    reason="policy-migration: schema-v2-provenance-required",
                    policy_id="policy-v1",
                    expected_sha256=self.source_digest,
                )

        result = self.store.retire_review(
            repository="owner/repo",
            commit=COMMIT,
            review_id="legacy",
            retirement_id=retirement_id,
            reason="policy-migration: schema-v2-provenance-required",
            policy_id="policy-v1",
            expected_sha256=self.source_digest,
        )
        self.assertEqual("FINALIZED", result["state"])

    def test_same_argument_replay_recovers_after_committed_source_move(self):
        original_append = self.store.append_event
        retirement_id = "retire-public-committed-gap"

        def stop_after_committed(event_type, *args, **kwargs):
            result = original_append(event_type, *args, **kwargs)
            if event_type == RETIREMENT_COMMITTED_EVENT:
                raise RuntimeError("crash after committed")
            return result

        with mock.patch.object(self.store, "append_event", side_effect=stop_after_committed):
            with self.assertRaises(RuntimeError):
                self.store.retire_review(
                    repository="owner/repo",
                    commit=COMMIT,
                    review_id="legacy",
                    retirement_id=retirement_id,
                    reason="policy-migration: schema-v2-provenance-required",
                    policy_id="policy-v1",
                    expected_sha256=self.source_digest,
                )
        self.store._move_no_replace(
            self.source,
            self.store.retirement_final_path(COMMIT, retirement_id),
            self.source_digest,
        )
        result = self.store.retire_review(
            repository="owner/repo",
            commit=COMMIT,
            review_id="legacy",
            retirement_id=retirement_id,
            reason="policy-migration: schema-v2-provenance-required",
            policy_id="policy-v1",
            expected_sha256=self.source_digest,
        )
        self.assertEqual("FINALIZED", result["state"])

    def test_same_byte_preplanted_staging_is_not_adopted_without_matching_sequence(self):
        retirement_id = "retire-collision"
        staging = self.store.retirement_staging_path(COMMIT, retirement_id)
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(self.source.read_bytes())
        with self.assertRaises((EvidenceConflict, MalformedEvidence)):
            self.store.retire_review(
                repository="owner/repo",
                commit=COMMIT,
                review_id="legacy",
                retirement_id=retirement_id,
                reason="policy-migration: schema-v2-provenance-required",
                policy_id="policy-v1",
                expected_sha256=self.source_digest,
            )

    def test_parent_fsync_failure_is_fatal(self):
        with mock.patch(
            "pr_closure.secure_paths._fsync_directory_fd",
            side_effect=OSError("simulated parent fsync failure"),
        ):
            with self.assertRaises(MalformedEvidence):
                self.store.retire_review(
                    repository="owner/repo",
                    commit=COMMIT,
                    review_id="legacy",
                    retirement_id="retire-fsync-failure",
                    reason="policy-migration: schema-v2-provenance-required",
                    policy_id="policy-v1",
                    expected_sha256=self.source_digest,
                )
        self.assertTrue(self.source.exists())
