import json
import os
import shutil
import tempfile
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
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40

CACHE_BASE = Path.home() / ".cache" / "pr-closure-store-test"


def _verification_record(commit=COMMIT_A):
    return {
        "schema_version": 1,
        "commit": commit,
        "completed_at": "2026-08-08T12:00:00+00:00",
        "commands": [{"command": "pytest", "exit_code": 0}],
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
        with self.assertRaises(MalformedEvidence):
            store.approval_status(COMMIT_A)

    def test_non_object_verification_artifact_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_verification(COMMIT_A, _verification_record())
        store.verification_path(COMMIT_A).write_text("[1, 2]\n")
        with self.assertRaises(MalformedEvidence):
            store.read_verification(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            store.approval_status(COMMIT_A)

    def test_malformed_review_artifact_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_review(COMMIT_A, "review-1", _review_record())
        store.review_path(COMMIT_A, "review-1").write_text("42")
        with self.assertRaises(MalformedEvidence):
            store.read_review(COMMIT_A, "review-1")
        with self.assertRaises(MalformedEvidence):
            store.approval_status(COMMIT_A)

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


if __name__ == "__main__":
    unittest.main()
