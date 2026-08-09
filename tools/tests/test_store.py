import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pr_closure.store import (
    COMMIT_EVENT,
    REVIEW_EVENT,
    VERIFICATION_EVENT,
    ApprovalState,
    EvidenceConflict,
    ForbiddenEvidencePath,
    MalformedEvidence,
    RunStore,
    StoreError,
    StorePathEscape,
    _serialize,
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40

CACHE_BASE = Path.home() / ".cache" / "pr-closure-store-test"


def _verification_record(commit=COMMIT_A, commands=None):
    """PASSED record in the digest-bound command format (C6D-F4/C6D-F2)."""
    if commands is None:
        commands = (
            ("local_review_ready", "typecheck"),
            ("closure_acceptance", "acceptance"),
        )
    return {
        "schema_version": 1,
        "commit": commit,
        "completed_at": "2026-08-08T12:00:00+00:00",
        "outcome": "PASSED",
        "commands": [
            _command_entry(phase, command, index)
            for index, (phase, command) in enumerate(commands, start=1)
        ],
    }


def _review_record(commit=COMMIT_A):
    return {
        "schema_version": 1,
        "repository": "owner/repo",
        "pr_number": 42,
        "reviewed_commit": commit,
        "implementer_family": "deepseek",
        "reviewer_family": "anthropic",
        "verdict": "APPROVED",
    }


def _digest(text):
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def _command_entry(phase, command, index, exit_status=0):
    return {
        "phase": phase,
        "command_digest": _digest(command),
        "command_label": "{0}:{1}".format(phase, index),
        "started_at": "2026-08-08T12:00:00+00:00",
        "ended_at": "2026-08-08T12:00:01+00:00",
        "exit_status": exit_status,
    }


def _new_verification_record(commit=COMMIT_A, commands=None):
    return _verification_record(commit=commit, commands=commands)


def _expected_commands(commands=None):
    """Exact ordered (phase, command_digest) sequence a config would require."""
    if commands is None:
        commands = (
            ("local_review_ready", "typecheck"),
            ("closure_acceptance", "acceptance"),
        )
    return tuple((phase, _digest(command)) for phase, command in commands)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        CACHE_BASE.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=CACHE_BASE))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def durable_file(self, name):
        path = self.root / "proof" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("durable evidence")
        return str(path)

    def _write_artifact(self, path, record):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_serialize(record))


class RunStorePathTests(StoreTestCase):
    def test_rejects_escaped_project_components(self):
        for bad in ("../etc", "a/b", "a\\b", "..", ".", "", "/abs", "x\x00", " proj"):
            with self.subTest(project=bad):
                with self.assertRaises(StorePathEscape):
                    RunStore(self.root, bad, 1)

    def test_rejects_non_positive_pr(self):
        for bad in (0, -1, 1.5, "7", True, None, False):
            with self.subTest(pr=bad):
                with self.assertRaises(StorePathEscape):
                    RunStore(self.root, "proj", bad)

    def test_rejects_escaped_review_id(self):
        store = RunStore(self.root, "proj", 7)
        for bad in ("a/b", "a\\b", "..", ".", "", "x\x00"):
            with self.subTest(review_id=bad):
                with self.assertRaises(StorePathEscape):
                    store.write_review(COMMIT_A, bad, _review_record())

    def test_rejects_relative_store_root(self):
        with self.assertRaises(ForbiddenEvidencePath):
            RunStore("relative/root", "proj", 1)

    def test_rejects_store_root_under_forbidden_areas(self):
        for root in (Path("/tmp/pcs-root"), Path(os.path.expanduser("~/.claude/jobs/pcs"))):
            with self.subTest(root=root):
                with self.assertRaises(ForbiddenEvidencePath):
                    RunStore(root, "proj", 1)

    def test_rejects_store_root_through_a_symlink_escape(self):
        link = self.root / "escaped-root"
        os.symlink("/tmp", link)
        with self.assertRaises(ForbiddenEvidencePath):
            RunStore(link, "proj", 1)

    def test_safe_project_and_pr_build_a_bounded_path(self):
        store = RunStore(self.root, "publyapp", 1078)
        self.assertEqual("publyapp", store.project)
        self.assertEqual(1078, store.pr)
        self.assertEqual(self.root / "publyapp" / "1078", store.base_dir)
        self.assertEqual(self.root / "publyapp" / "1078" / "events.jsonl", store.events_path)


class AppendOnlyEventTests(StoreTestCase):
    def test_appending_does_not_rewrite_prior_bytes(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        first = store.events_path.read_bytes()
        store.write_review(COMMIT_A, "review-1", _review_record())
        store.record_commit(COMMIT_B, self.durable_file("tip-b"))
        second = store.events_path.read_bytes()
        self.assertTrue(second.startswith(first))
        self.assertTrue(first.startswith(b"{"))
        self.assertEqual(4, len(second.decode("utf-8").splitlines()))
        for line in second.decode("utf-8").splitlines():
            json.loads(line)

    def test_every_event_carries_the_required_keys(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        events = store.read_events()
        self.assertEqual({COMMIT_EVENT, VERIFICATION_EVENT, REVIEW_EVENT}, {e["event_type"] for e in events})
        for event in events:
            with self.subTest(event=event["event_type"]):
                for key in ("schema_version", "timestamp", "project", "pr", "commit", "event_type", "evidence_path"):
                    self.assertIn(key, event)
                self.assertEqual(1, event["schema_version"])
                self.assertEqual("proj", event["project"])
                self.assertEqual(42, event["pr"])

    def test_timestamps_are_utc_and_parseable(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        event = store.read_events()[0]
        parsed = datetime.fromisoformat(event["timestamp"])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(timedelta(0), parsed.utcoffset())
        self.assertLessEqual(parsed - datetime.now(timezone.utc), timedelta(minutes=1))

    def test_record_writes_append_evidence_events(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        events = store.read_events()
        self.assertEqual([COMMIT_EVENT, VERIFICATION_EVENT, REVIEW_EVENT], [e["event_type"] for e in events])
        self.assertEqual(str(store.verification_path(COMMIT_A)), events[1]["evidence_path"])
        self.assertEqual(str(store.review_path(COMMIT_A, "review-1")), events[2]["evidence_path"])
        self.assertEqual(COMMIT_A, events[1]["commit"])
        self.assertEqual(COMMIT_A, events[2]["commit"])

    def test_payload_preserves_explicit_evidence_for_later_tasks(self):
        store = RunStore(self.root, "proj", 42)
        store.append_event(
            "STAGNATION_CONFIG",
            COMMIT_A,
            self.durable_file("config"),
            payload={"stagnation_budget_minutes": 240, "infra_retry_budget": 1},
        )
        store.append_event(
            "REPAIR_STRATEGY",
            COMMIT_A,
            self.durable_file("repair"),
            payload={"root_cause": "missing-invariant", "strategy": "syntax-patch-1"},
        )
        events = store.read_events()
        self.assertEqual(240, events[0]["stagnation_budget_minutes"])
        self.assertEqual(1, events[0]["infra_retry_budget"])
        self.assertEqual("REPAIR_STRATEGY", events[1]["event_type"])
        self.assertEqual("missing-invariant", events[1]["root_cause"])
        self.assertEqual("syntax-patch-1", events[1]["strategy"])


class AtomicRecordWriteTests(StoreTestCase):
    def test_verification_lives_at_verification_commit_json(self):
        store = RunStore(self.root, "proj", 42)
        store.write_verification(COMMIT_A, _verification_record())
        path = store.verification_path(COMMIT_A)
        self.assertTrue(path.is_file())
        self.assertEqual("verification", path.parent.name)
        self.assertEqual(COMMIT_A + ".json", path.name)
        self.assertEqual(_verification_record(), json.loads(path.read_text()))
        self.assertEqual([path.name], [p.name for p in path.parent.iterdir()])

    def test_reviews_live_at_reviews_commit_review_id_json(self):
        store = RunStore(self.root, "proj", 42)
        store.write_review(COMMIT_A, "review-1", _review_record())
        path = store.review_path(COMMIT_A, "review-1")
        self.assertTrue(path.is_file())
        self.assertEqual("reviews", path.parent.parent.name)
        self.assertEqual(COMMIT_A, path.parent.name)
        self.assertEqual("review-1.json", path.name)
        self.assertEqual(_review_record(), json.loads(path.read_text()))

    def test_atomic_write_leaves_no_temporary_files_behind(self):
        store = RunStore(self.root, "proj", 42)
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        leftovers = []
        for directory in (store.base_dir, store.verification_path(COMMIT_A).parent, store.review_dir(COMMIT_A)):
            leftovers.extend(str(p) for p in directory.glob("*.tmp*"))
            leftovers.extend(str(p) for p in directory.glob(".tmp*"))
        self.assertEqual([], leftovers)

    def test_verification_is_never_silently_overwritten(self):
        store = RunStore(self.root, "proj", 42)
        record = _verification_record()
        store.write_verification(COMMIT_A, record)
        store.write_verification(COMMIT_A, record)
        with self.assertRaises(EvidenceConflict):
            store.write_verification(COMMIT_A, {"schema_version": 1, "commit": COMMIT_A, "ok": False})
        self.assertEqual(record, store.read_verification(COMMIT_A))


class ReviewIdCollisionTests(StoreTestCase):
    def test_identical_review_bytes_may_be_written_again(self):
        store = RunStore(self.root, "proj", 42)
        store.write_review(COMMIT_A, "review-1", _review_record())
        store.write_review(COMMIT_A, "review-1", _review_record())

    def test_different_review_bytes_raise_and_preserve_the_original(self):
        store = RunStore(self.root, "proj", 42)
        original = _review_record()
        store.write_review(COMMIT_A, "review-1", original)
        changed = dict(original, verdict="CHANGES_REQUIRED")
        with self.assertRaises(EvidenceConflict):
            store.write_review(COMMIT_A, "review-1", changed)
        self.assertEqual(original, json.loads(store.review_path(COMMIT_A, "review-1").read_text()))

    def test_collision_is_per_commit_and_per_review_id(self):
        store = RunStore(self.root, "proj", 42)
        store.write_review(COMMIT_A, "review-1", _review_record(COMMIT_A))
        store.write_review(COMMIT_B, "review-1", _review_record(COMMIT_B))
        store.write_review(COMMIT_A, "review-2", _review_record(COMMIT_A))


class ForbiddenEvidencePathTests(StoreTestCase):
    def test_rejects_tmp_as_evidence_path(self):
        store = RunStore(self.root, "proj", 42)
        for path in ("/tmp", "/tmp/evidence.json", "/tmp/a/b/evidence.json"):
            with self.subTest(path=path):
                with self.assertRaises(ForbiddenEvidencePath):
                    store.record_commit(COMMIT_A, path)

    def test_rejects_claude_jobs_as_evidence_path(self):
        store = RunStore(self.root, "proj", 42)
        for path in (
            "~/.claude/jobs",
            "~/.claude/jobs/session/x/evidence.json",
            os.path.join(os.path.expanduser("~/.claude/jobs"), "x.json"),
        ):
            with self.subTest(path=path):
                with self.assertRaises(ForbiddenEvidencePath):
                    store.record_commit(COMMIT_A, path)

    def test_allows_lookalike_roots_that_are_not_descendants(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("ok"))
        store.record_commit(COMMIT_A, "/tmpx/evidence.json")
        store.record_commit(COMMIT_A, os.path.join(os.path.expanduser("~/.claude/jobs-archive"), "x.json"))

    def test_rejects_relative_evidence_path(self):
        store = RunStore(self.root, "proj", 42)
        with self.assertRaises(ForbiddenEvidencePath):
            store.record_commit(COMMIT_A, "relative/evidence.json")

    def test_rejects_symlink_escape_into_tmp(self):
        store = RunStore(self.root, "proj", 42)
        link = self.root / "escape"
        os.symlink("/tmp", link)
        with self.assertRaises(ForbiddenEvidencePath):
            store.record_commit(COMMIT_A, str(link / "evidence.json"))

    def test_allows_symlink_resolution_outside_forbidden_roots(self):
        store = RunStore(self.root, "proj", 42)
        outside = Path(tempfile.mkdtemp(dir=CACHE_BASE))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        link = self.root / "fine-link"
        os.symlink(outside, link)
        store.record_commit(COMMIT_A, str(link / "evidence.json"))


class StaleApprovalTests(StoreTestCase):
    def test_newer_commit_event_invalidates_older_approval(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(ApprovalState.APPROVED, store.approval_status(COMMIT_A))
        store.record_commit(COMMIT_B, self.durable_file("tip-b"))
        self.assertEqual(COMMIT_B, store.current_commit())
        self.assertEqual(ApprovalState.STALE, store.approval_status(COMMIT_A))
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_B))

    def test_approval_requires_both_verification_and_review(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        store.write_verification(COMMIT_A, _verification_record())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(ApprovalState.APPROVED, store.approval_status(COMMIT_A))

    def test_unknown_commit_without_any_evidence_is_unverified(self):
        store = RunStore(self.root, "proj", 42)
        self.assertIsNone(store.current_commit())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))


class MissingArtifactFailsClosedTests(StoreTestCase):
    def test_missing_referenced_artifact_never_returns_favorable_state(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        store.write_verification(COMMIT_A, _verification_record())
        self.assertEqual(ApprovalState.APPROVED, store.approval_status(COMMIT_A))
        store.verification_path(COMMIT_A).unlink()
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        self.assertFalse(store.verification_exists(COMMIT_A))


class MalformedDataFailsClosedTests(StoreTestCase):
    def test_malformed_event_line_raises_everywhere(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        with store.events_path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        with self.assertRaises(MalformedEvidence):
            store.read_events()
        with self.assertRaises(MalformedEvidence):
            store.current_commit()
        with self.assertRaises(MalformedEvidence):
            store.approval_status(COMMIT_A)

    def test_non_object_event_line_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        with store.events_path.open("a", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]\n")
        with self.assertRaises(MalformedEvidence):
            store.read_events()

    def test_event_missing_required_keys_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        with store.events_path.open("a", encoding="utf-8") as handle:
            handle.write('{"schema_version": 1}\n')
        with self.assertRaises(MalformedEvidence):
            store.read_events()

    def test_malformed_verification_artifact_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        store.verification_path(COMMIT_A).write_text("{broken")
        with self.assertRaises(MalformedEvidence):
            store.read_verification(COMMIT_A)
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_non_object_verification_artifact_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        store.verification_path(COMMIT_A).write_text("[1, 2]\n")
        with self.assertRaises(MalformedEvidence):
            store.read_verification(COMMIT_A)
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_malformed_review_artifact_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_review(COMMIT_A, "review-1", _review_record())
        store.review_path(COMMIT_A, "review-1").write_text("42")
        with self.assertRaises(MalformedEvidence):
            store.read_review(COMMIT_A, "review-1")
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_contradictory_record_commits_are_rejected(self):
        store = RunStore(self.root, "proj", 42)
        with self.assertRaises(MalformedEvidence):
            store.write_verification(COMMIT_A, _verification_record(COMMIT_B))
        with self.assertRaises(MalformedEvidence):
            store.write_review(COMMIT_A, "review-1", _review_record(COMMIT_B))

    def test_malformed_commit_is_rejected(self):
        store = RunStore(self.root, "proj", 42)
        for bad in ("", "   ", "a" * 39, "A" * 40, "not-a-commit", None, 42):
            with self.subTest(commit=bad):
                with self.assertRaises((StoreError, TypeError)):
                    store.record_commit(bad, self.durable_file("x"))
                with self.assertRaises((StoreError, TypeError)):
                    store.approval_status(bad)


class BoundArtifactSeamTests(StoreTestCase):
    """C6C-F4: only event-bound durable artifacts are authority."""

    def test_bound_verification_absent_returns_none(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands()))

    def test_bound_verification_returns_event_bound_record(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _verification_record())
        self.assertEqual(_verification_record(), store.bound_verification(COMMIT_A, _expected_commands()))

    def test_orphan_verification_artifact_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        self._write_artifact(store.verification_path(COMMIT_A), _verification_record())
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_wrong_evidence_path_binding_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        other = Path(self.durable_file("other-verification"))
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(other),
            content_digest=_digest(b"durable evidence"),
        )
        self._write_artifact(store.verification_path(COMMIT_A), _verification_record())
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_missing_artifact_never_returns_bytes(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _verification_record())
        store.verification_path(COMMIT_A).unlink()
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands()))

    def test_symlink_replacement_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _verification_record())
        target = store.verification_path(COMMIT_A)
        decoy = self.root / "decoy.json"
        decoy.write_text(json.dumps(_verification_record()))
        target.unlink()
        target.symlink_to(decoy)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_replaced_record_with_contradictory_commit_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _verification_record())
        target = store.verification_path(COMMIT_A)
        replaced = dict(_verification_record(COMMIT_A), commit=COMMIT_B)
        target.write_text(_serialize(replaced).decode())
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_failed_outcome_artifact_is_never_authority(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _verification_record())
        target = store.verification_path(COMMIT_A)
        failed = dict(_verification_record(), outcome="FAILED")
        target.write_text(_serialize(failed).decode())
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_passed_record_must_prove_both_phases(self):
        cases = (
            ("empty", [], MalformedEvidence),
            ("single_phase", [_command_entry("local_review_ready", "x", 1)], None),
            ("missing_digest", [{"phase": "local_review_ready", "exit_status": 0}], MalformedEvidence),
            ("unknown_phase", [_command_entry("bogus_phase", "x", 1)], MalformedEvidence),
        )
        for label, commands, expected in cases:
            with self.subTest(case=label):
                root = Path(tempfile.mkdtemp(dir=self.root))
                store = RunStore(root, "proj", 42)
                store.record_commit(COMMIT_A, self.durable_file("tip"))
                mutated = dict(_verification_record(), commands=commands)
                self._write_artifact(store.verification_path(COMMIT_A), mutated)
                store.write_verification(COMMIT_A, mutated)
                if expected is None:
                    self.assertIsNone(
                        store.bound_verification(COMMIT_A, _expected_commands())
                    )
                else:
                    with self.assertRaises(expected):
                        store.bound_verification(COMMIT_A, _expected_commands())

    def test_bound_reviews_returns_event_bound_records(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_review(COMMIT_A, "r1", _review_record())
        bound = store.bound_reviews(COMMIT_A)
        self.assertEqual(1, len(bound))
        self.assertEqual("r1", bound[0][0])
        self.assertEqual(_review_record(), bound[0][1])

    def test_orphan_review_artifact_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        self._write_artifact(store.review_path(COMMIT_A, "orphan"), _review_record())
        with self.assertRaises(MalformedEvidence):
            store.bound_reviews(COMMIT_A)

    def test_one_orphan_review_poisons_all_reviews(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_review(COMMIT_A, "r1", _review_record())
        self._write_artifact(store.review_path(COMMIT_A, "orphan"), _review_record())
        with self.assertRaises(MalformedEvidence):
            store.bound_reviews(COMMIT_A)

    def test_symlink_review_artifact_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_review(COMMIT_A, "r1", _review_record())
        target = store.review_path(COMMIT_A, "r1")
        decoy = self.root / "review-decoy.json"
        decoy.write_text(json.dumps(_review_record()))
        target.unlink()
        target.symlink_to(decoy)
        with self.assertRaises(MalformedEvidence):
            store.bound_reviews(COMMIT_A)

    def test_missing_review_returns_empty(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        self.assertEqual((), store.bound_reviews(COMMIT_A))


class ConcurrencyAndPermissionsTests(StoreTestCase):
    def test_events_file_is_created_0600(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self.assertEqual(0o600, store.events_path.stat().st_mode & 0o777)

    def test_record_files_are_created_0600(self):
        store = RunStore(self.root, "proj", 42)
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(0o600, store.verification_path(COMMIT_A).stat().st_mode & 0o777)
        self.assertEqual(0o600, store.review_path(COMMIT_A, "review-1").stat().st_mode & 0o777)

    def test_concurrent_appends_are_not_corrupted(self):
        store = RunStore(self.root, "proj", 42)
        total = 64

        def worker(i):
            commit = "%040x" % i
            store.record_commit(commit, self.durable_file("w%d" % i), payload={"worker": i})

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(worker, range(total)))
        events = store.read_events()
        self.assertEqual(total, len(events))
        self.assertEqual(set(range(total)), {event["worker"] for event in events})
        raw = store.events_path.read_text(encoding="utf-8")
        self.assertEqual(total, len(raw.splitlines()))
        self.assertTrue(raw.endswith("\n"))
        for event in events:
            self.assertIn(event["commit"], {("%040x" % i) for i in range(total)})


class PayloadCannotOverrideEnvelopeTests(StoreTestCase):
    RESERVED_KEYS = (
        "schema_version",
        "timestamp",
        "project",
        "pr",
        "commit",
        "event_type",
        "evidence_path",
        "content_digest",
    )

    def test_reproduction_payload_is_rejected_before_any_bytes(self):
        store = RunStore(self.root, "proj", 42)
        with self.assertRaises(MalformedEvidence):
            store.append_event(
                "commit",
                COMMIT_A,
                self.durable_file("tip"),
                payload={
                    "commit": COMMIT_B,
                    "project": "other",
                    "pr": 999,
                    "evidence_path": "/tmp/not-authoritative",
                },
            )
        self.assertFalse(store.events_path.exists())

    def test_each_reserved_key_is_rejected_before_any_bytes(self):
        store = RunStore(self.root, "proj", 42)
        for key in self.RESERVED_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(MalformedEvidence):
                    store.append_event(
                        "REPAIR_STRATEGY",
                        COMMIT_A,
                        self.durable_file("tip"),
                        payload={key: "hijacked"},
                    )
                self.assertFalse(store.events_path.exists())

    def test_byte_identical_reserved_values_are_also_rejected(self):
        store = RunStore(self.root, "proj", 42)
        identical = (
            ("commit", COMMIT_A),
            ("project", "proj"),
            ("pr", 42),
            ("schema_version", 1),
            ("event_type", "REPAIR_STRATEGY"),
        )
        for key, value in identical:
            with self.subTest(key=key):
                with self.assertRaises(MalformedEvidence):
                    store.append_event(
                        "REPAIR_STRATEGY",
                        COMMIT_A,
                        self.durable_file("tip"),
                        payload={key: value},
                    )
                self.assertFalse(store.events_path.exists())

    def test_append_rejects_unknown_event_type_before_any_bytes(self):
        store = RunStore(self.root, "proj", 42)
        with self.assertRaises(MalformedEvidence):
            store.append_event("NOT_A_KNOWN_TYPE", COMMIT_A, self.durable_file("tip"))
        self.assertFalse(store.events_path.exists())

    def test_payload_with_no_reserved_keys_is_preserved(self):
        store = RunStore(self.root, "proj", 42)
        store.append_event(
            "REPAIR_STRATEGY",
            COMMIT_A,
            self.durable_file("tip"),
            payload={"root_cause": "x"},
        )
        event = store.read_events()[0]
        self.assertEqual("REPAIR_STRATEGY", event["event_type"])
        self.assertEqual(COMMIT_A, event["commit"])
        self.assertEqual("proj", event["project"])
        self.assertEqual(42, event["pr"])
        self.assertEqual("x", event["root_cause"])


class ReadEventsBindsEnvelopeTests(StoreTestCase):
    def _envelope(self, **overrides):
        base = {
            "schema_version": 1,
            "timestamp": "2026-08-08T12:00:00+00:00",
            "project": "proj",
            "pr": 42,
            "commit": COMMIT_A,
            "event_type": COMMIT_EVENT,
            "evidence_path": self.durable_file("tip"),
        }
        base.update(overrides)
        return base

    def _inject(self, store, event):
        path = json.dumps(event, sort_keys=True) + "\n"
        store.events_path.parent.mkdir(parents=True, exist_ok=True)
        store.events_path.write_text(path)

    def test_read_rejects_foreign_project(self):
        store = RunStore(self.root, "proj", 42)
        self._inject(store, self._envelope(project="other"))
        with self.assertRaises(MalformedEvidence):
            store.read_events()

    def test_read_rejects_foreign_pr(self):
        store = RunStore(self.root, "proj", 42)
        self._inject(store, self._envelope(pr=999))
        with self.assertRaises(MalformedEvidence):
            store.read_events()

    def test_read_requires_exact_schema_version(self):
        store = RunStore(self.root, "proj", 42)
        for bad in (2, 0, "1", 1.0, True):
            with self.subTest(schema_version=bad):
                self._inject(store, self._envelope(schema_version=bad))
                with self.assertRaises(MalformedEvidence):
                    store.read_events()

    def test_read_requires_known_event_type(self):
        store = RunStore(self.root, "proj", 42)
        for bad in ("FOREIGN_EVENT", "commit-extra", "commit ", 1):
            with self.subTest(event_type=bad):
                self._inject(store, self._envelope(event_type=bad))
                with self.assertRaises(MalformedEvidence):
                    store.read_events()

    def test_read_requires_valid_utc_timestamp(self):
        store = RunStore(self.root, "proj", 42)
        for bad in ("2026-08-08T12:00:00", "not-a-timestamp", "2026-08-08T12:00:00+02:00", 12345, None):
            with self.subTest(timestamp=bad):
                self._inject(store, self._envelope(timestamp=bad))
                with self.assertRaises(MalformedEvidence):
                    store.read_events()

    def test_read_requires_durable_absolute_evidence_path(self):
        store = RunStore(self.root, "proj", 42)
        for bad in ("/tmp/evidence.json", "relative/evidence.json", "", 42, None):
            with self.subTest(evidence_path=bad):
                self._inject(store, self._envelope(evidence_path=bad))
                with self.assertRaises(MalformedEvidence):
                    store.read_events()

    def test_read_requires_correctly_typed_values(self):
        store = RunStore(self.root, "proj", 42)
        overrides = (
            {"project": 42},
            {"pr": "42"},
            {"pr": 42.0},
            {"pr": True},
            {"commit": "a" * 39},
            {"commit": 42},
        )
        for change in overrides:
            with self.subTest(overrides=change):
                self._inject(store, self._envelope(**change))
                with self.assertRaises(MalformedEvidence):
                    store.read_events()

    def test_read_rejects_foreign_envelope_from_approval_paths(self):
        store = RunStore(self.root, "proj", 42)
        self._inject(store, self._envelope(project="other"))
        with self.assertRaises(MalformedEvidence):
            store.current_commit()
        with self.assertRaises(MalformedEvidence):
            store.approval_status(COMMIT_A)

    def test_read_binds_valid_envelope_values(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        event = store.read_events()[0]
        self.assertEqual(1, event["schema_version"])
        self.assertEqual("proj", event["project"])
        self.assertEqual(42, event["pr"])
        self.assertEqual(COMMIT_A, event["commit"])
        self.assertEqual(COMMIT_EVENT, event["event_type"])
        self.assertEqual(self.durable_file("tip"), event["evidence_path"])


class TipObservedBeforeApprovalTests(StoreTestCase):
    def test_approval_requires_an_observed_pushed_tip(self):
        store = RunStore(self.root, "proj", 42)
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertIsNone(store.current_commit())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_approval_is_approved_once_the_tip_is_observed(self):
        store = RunStore(self.root, "proj", 42)
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self.assertEqual(ApprovalState.APPROVED, store.approval_status(COMMIT_A))

    def test_missing_current_tip_evidence_never_approves(self):
        store = RunStore(self.root, "proj", 42)
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_stale_requires_valid_artifacts_for_the_queried_commit(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.record_commit(COMMIT_B, self.durable_file("tip-b"))
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        store.write_verification(COMMIT_A, _verification_record())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(ApprovalState.STALE, store.approval_status(COMMIT_A))


class OrphanEvidenceFailsClosedTests(StoreTestCase):
    """T3R-F1: a failed event append must not leave approval evidence."""

    def test_failed_event_append_raises_and_rolls_back_the_artifact(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        os.chmod(store.events_path, 0o400)
        try:
            with self.assertRaises(OSError):
                store.write_verification(COMMIT_A, _verification_record())
            self.assertFalse(store.verification_path(COMMIT_A).exists())
            self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        finally:
            os.chmod(store.events_path, 0o600)

    def test_failed_append_then_restored_retry_reaches_approved(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        os.chmod(store.events_path, 0o400)
        try:
            with self.assertRaises(OSError):
                store.write_verification(COMMIT_A, _verification_record())
        finally:
            os.chmod(store.events_path, 0o600)
        store.write_verification(COMMIT_A, _verification_record())
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(
            [COMMIT_EVENT, VERIFICATION_EVENT, REVIEW_EVENT],
            [e["event_type"] for e in store.read_events()],
        )
        self.assertEqual(ApprovalState.APPROVED, store.approval_status(COMMIT_A))

    def test_orphan_artifacts_without_matching_events_never_approve(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self._write_artifact(store.verification_path(COMMIT_A), _verification_record())
        self._write_artifact(store.review_path(COMMIT_A, "review-1"), _review_record())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_failed_repair_append_leaves_an_orphan_that_cannot_approve(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self._write_artifact(store.verification_path(COMMIT_A), _verification_record())
        os.chmod(store.events_path, 0o400)
        try:
            with self.assertRaises(OSError):
                store.write_verification(COMMIT_A, _verification_record())
            self.assertTrue(store.verification_path(COMMIT_A).is_file())
            self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        finally:
            os.chmod(store.events_path, 0o600)

    def test_identical_retry_repairs_a_missing_event(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self._write_artifact(store.verification_path(COMMIT_A), _verification_record())
        store.write_verification(COMMIT_A, _verification_record())
        self.assertEqual(
            [COMMIT_EVENT, VERIFICATION_EVENT], [e["event_type"] for e in store.read_events()]
        )
        store.write_verification(COMMIT_A, _verification_record())
        self.assertEqual(2, len(store.read_events()))

    def test_already_complete_identical_write_adds_no_duplicate_event(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        store.write_verification(COMMIT_A, _verification_record())
        self.assertEqual(
            [COMMIT_EVENT, VERIFICATION_EVENT], [e["event_type"] for e in store.read_events()]
        )

    def test_write_rejects_verification_dir_symlinked_into_tmp_before_any_write(self):
        session = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, session, ignore_errors=True)
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        os.symlink(session, store.verification_path(COMMIT_A).parent)
        with self.assertRaises(ForbiddenEvidencePath):
            store.write_verification(COMMIT_A, _verification_record())
        self.assertEqual([], list(session.iterdir()))

    def test_write_rejects_reviews_dir_symlinked_into_tmp_before_any_write(self):
        session = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, session, ignore_errors=True)
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        os.symlink(session, store.review_dir(COMMIT_A).parent)
        with self.assertRaises(ForbiddenEvidencePath):
            store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual([], list(session.iterdir()))

    def test_approval_requires_a_matching_verification_event_for_the_resolved_path(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self._write_artifact(store.verification_path(COMMIT_A), _verification_record())
        self._write_artifact(store.review_path(COMMIT_A, "review-1"), _review_record())
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            self.durable_file("elsewhere"),
            content_digest=_digest(b"durable evidence"),
        )
        store.append_event(
            REVIEW_EVENT,
            COMMIT_A,
            str(store.review_path(COMMIT_A, "review-1")),
            content_digest=_digest(_serialize(_review_record())),
        )
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_approval_requires_a_matching_review_event(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        self._write_artifact(store.review_path(COMMIT_A, "review-1"), _review_record())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(ApprovalState.APPROVED, store.approval_status(COMMIT_A))


class ConcurrentRecordWriteTests(StoreTestCase):
    """T3R-F2: atomic no-replace record create under concurrent different-byte writers."""

    def test_concurrent_different_review_bytes_cannot_both_succeed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        original = _review_record()
        changed = dict(original, verdict="CHANGES_REQUIRED")
        for round_id in range(5):
            review_id = "rev-%d" % round_id
            barrier = threading.Barrier(2)
            outcomes = []

            def worker(record):
                barrier.wait()
                try:
                    store.write_review(COMMIT_A, review_id, record)
                    outcomes.append("ok")
                except EvidenceConflict:
                    outcomes.append("conflict")

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(worker, (original, changed)))
            self.assertEqual(1, outcomes.count("ok"), outcomes)
            self.assertEqual(1, outcomes.count("conflict"), outcomes)
            stored = json.loads(store.review_path(COMMIT_A, review_id).read_text())
            self.assertIn(stored, (original, changed))
            review_events = [
                e
                for e in store.read_events()
                if e["event_type"] == REVIEW_EVENT
                and e["commit"] == COMMIT_A
                and e["evidence_path"] == str(store.review_path(COMMIT_A, review_id))
            ]
            self.assertEqual(1, len(review_events))

    def test_concurrent_identical_review_bytes_may_both_succeed_idempotently(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                store.write_review(COMMIT_A, "rev-same", _review_record())
                outcomes.append("ok")
            except EvidenceConflict:
                outcomes.append("conflict")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(2, outcomes.count("ok"))
        self.assertEqual(_review_record(), json.loads(store.review_path(COMMIT_A, "rev-same").read_text()))
        events = store.read_events()
        self.assertEqual({COMMIT_EVENT, VERIFICATION_EVENT, REVIEW_EVENT}, {e["event_type"] for e in events})
        for event in events:
            self.assertIn(event["event_type"], (COMMIT_EVENT, VERIFICATION_EVENT, REVIEW_EVENT))
        self.assertEqual(ApprovalState.APPROVED, store.approval_status(COMMIT_A))

    def test_concurrent_different_verification_bytes_cannot_both_succeed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        original = _verification_record()
        changed = dict(original, commands=[{"command": "pytest", "exit_code": 1}])
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(record):
            barrier.wait()
            try:
                store.write_verification(COMMIT_A, record)
                outcomes.append("ok")
            except EvidenceConflict:
                outcomes.append("conflict")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, (original, changed)))
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(1, outcomes.count("conflict"))
        stored = json.loads(store.verification_path(COMMIT_A).read_text())
        self.assertIn(stored, (original, changed))

    @unittest.skipUnless(hasattr(os, "fork"), "requires os.fork")
    def test_cross_process_different_review_bytes_cannot_both_succeed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        original = _review_record()
        changed = dict(original, verdict="CHANGES_REQUIRED")
        for round_id in range(2):
            review_id = "proc-%d" % round_id
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                try:
                    child_store = RunStore(self.root, "proj", 42)
                    child_store.write_review(COMMIT_A, review_id, changed)
                    result = b"ok"
                except EvidenceConflict:
                    result = b"conflict"
                except Exception as error:
                    result = ("error:" + type(error).__name__).encode("ascii")
                try:
                    os.write(w, result)
                finally:
                    os.close(w)
                os._exit(0)
            os.close(w)
            try:
                store.write_review(COMMIT_A, review_id, original)
                parent_outcome = "ok"
            except EvidenceConflict:
                parent_outcome = "conflict"
            child_outcome = os.read(r, 256).decode("ascii")
            os.close(r)
            os.waitpid(pid, 0)
            self.assertEqual(["conflict", "ok"], sorted((child_outcome, parent_outcome)))
            stored = json.loads(store.review_path(COMMIT_A, review_id).read_text())
            self.assertIn(stored, (original, changed))



class ConfigExactSequenceBindingTests(StoreTestCase):
    """C6D-F2: green verification requires the exact ordered config sequence.

    A truncated, reordered, duplicated, substituted, nonzero, boolean, or
    old-config command record must never derive approval.
    """

    def _green(self, commands=None):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _new_verification_record(commands=commands))
        return store

    def test_exact_current_sequence_is_green(self):
        store = self._green()
        record = store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual("PASSED", record["outcome"])

    def test_omitted_command_fails_closed(self):
        store = self._green(commands=(("local_review_ready", "typecheck"),))
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands()))
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_extra_command_fails_closed(self):
        store = self._green(
            commands=(
                ("local_review_ready", "typecheck"),
                ("closure_acceptance", "acceptance"),
                ("closure_acceptance", "extra"),
            )
        )
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands()))

    def test_reordered_command_fails_closed(self):
        store = self._green(
            commands=(
                ("closure_acceptance", "acceptance"),
                ("local_review_ready", "typecheck"),
            )
        )
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands()))

    def test_duplicate_phase_used_as_substitute_fails_closed(self):
        store = self._green(
            commands=(
                ("local_review_ready", "typecheck"),
                ("local_review_ready", "typecheck"),
            )
        )
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands()))

    def test_nonzero_status_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        record = _new_verification_record()
        record["commands"][1]["exit_status"] = 1
        store.write_verification(COMMIT_A, record)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_boolean_status_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        record = _new_verification_record()
        record["commands"][0]["exit_status"] = True
        store.write_verification(COMMIT_A, record)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_changed_config_command_makes_old_evidence_stale(self):
        store = self._green()
        changed = (
            ("local_review_ready", "typecheck2"),
            ("closure_acceptance", "acceptance"),
        )
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands(changed)))

    def test_changed_phase_makes_old_evidence_stale(self):
        store = self._green()
        changed = (
            ("closure_acceptance", "typecheck"),
            ("closure_acceptance", "acceptance"),
        )
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands(changed)))


class ContentDigestBindingTests(StoreTestCase):
    """C6D-F3: verification and review events bind artifact bytes, not paths."""

    def test_verification_event_carries_exact_lowercase_hex_digest(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _new_verification_record())
        event = store.read_events()[-1]
        digest = event["content_digest"]
        self.assertEqual(64, len(digest))
        self.assertRegex(digest, "^[0-9a-f]{64}$")
        expected = hashlib.sha256(store.verification_path(COMMIT_A).read_bytes()).hexdigest()
        self.assertEqual(expected, digest)
        self.assertEqual(COMMIT_A, event["commit"])

    def test_review_event_carries_exact_lowercase_hex_digest(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_review(COMMIT_A, "r1", _review_record())
        event = store.read_events()[-1]
        digest = event["content_digest"]
        self.assertEqual(64, len(digest))
        self.assertRegex(digest, "^[0-9a-f]{64}$")
        expected = hashlib.sha256(store.review_path(COMMIT_A, "r1").read_bytes()).hexdigest()
        self.assertEqual(expected, digest)
        self.assertEqual(COMMIT_A, event["commit"])

    def test_same_path_regular_file_verification_replacement_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _new_verification_record())
        replaced = _new_verification_record()
        replaced["commands"][0]["command_label"] = "local_review_ready:9"
        store.verification_path(COMMIT_A).write_text(_serialize(replaced).decode())
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_same_path_review_replacement_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_review(COMMIT_A, "r1", _review_record())
        replaced = dict(_review_record(), verdict="CHANGES_REQUIRED")
        store.review_path(COMMIT_A, "r1").write_text(_serialize(replaced).decode())
        with self.assertRaises(MalformedEvidence):
            store.bound_reviews(COMMIT_A)
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_legacy_path_only_verification_event_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _new_verification_record())
        events = list(store.read_events())
        events[-1].pop("content_digest")
        payload = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        store.events_path.write_text(payload)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())
        with self.assertRaises(MalformedEvidence):
            store.approval_status(COMMIT_A)

    def test_malformed_digest_event_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _new_verification_record())
        events = list(store.read_events())
        events[-1]["content_digest"] = "NOT_HEX_DIGEST"
        payload = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        store.events_path.write_text(payload)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_wrong_digest_event_never_binds_authority(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _new_verification_record())
        events = list(store.read_events())
        events[-1] = {
            "schema_version": 1,
            "timestamp": "2026-08-08T12:00:00+00:00",
            "project": "proj",
            "pr": 42,
            "commit": COMMIT_A,
            "event_type": VERIFICATION_EVENT,
            "evidence_path": str(store.verification_path(COMMIT_A)),
            "content_digest": _digest("forged bytes"),
        }
        payload = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        store.events_path.write_text(payload)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual(ApprovalState.UNVERIFIED, store.approval_status(COMMIT_A))

    def test_identical_retry_remains_idempotent_with_digests(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, _new_verification_record())
        store.write_verification(COMMIT_A, _new_verification_record())
        store.write_review(COMMIT_A, "r1", _review_record())
        events = [e for e in store.read_events() if e["event_type"] == VERIFICATION_EVENT]
        self.assertEqual(1, len(events))
        record = store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual("PASSED", record["outcome"])
        self.assertEqual(ApprovalState.APPROVED, store.approval_status(COMMIT_A))

    def test_failed_verification_event_needs_no_digest_and_stays_readable(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(store.events_path),
            payload={
                "outcome": "FAILED",
                "failed_command_digest": _digest("boom"),
                "commands": [_command_entry("local_review_ready", "boom", 1, exit_status=9)],
            },
        )
        event = store.read_events()[-1]
        self.assertEqual("FAILED", event["outcome"])
        self.assertNotIn("content_digest", event)
        self.assertEqual(_digest("boom"), event["failed_command_digest"])



if __name__ == "__main__":
    unittest.main()
