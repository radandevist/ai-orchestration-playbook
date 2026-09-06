import hashlib
import json
import shutil
import tempfile
import unittest
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
