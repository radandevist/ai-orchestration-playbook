import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

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
