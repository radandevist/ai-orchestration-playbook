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

from pr_closure.contract import command_sequence_digest, legacy_command_sequence_digest
from pr_closure.model import ClosureState
from pr_closure.store import (
    COMMIT_EVENT,
    REPAIR_STRATEGY_EVENT,
    REVIEW_DISPATCH_EVENT,
    REVIEW_EVENT,
    VERIFICATION_EVENT,
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


def _verification_record(
    commit=COMMIT_A,
    config_digest=None,
    commands=None,
    started_at="2026-08-08T12:00:00+00:00",
    ended_at="2026-08-08T12:00:01+00:00",
):
    """PASSED attempt record in the digest-bound command format (C6D-F4/C6D-F2).

    Carries the immutable ``config_digest`` identity so multiple attempts and
    configurations can coexist at one commit (T6L-F2).
    """
    if commands is None:
        commands = DEFAULT_COMMANDS
    if config_digest is None:
        config_digest = _config_digest_of(commands)
    return {
        "schema_version": 1,
        "commit": commit,
        "config_digest": config_digest,
        "completed_at": started_at,
        "verification_command_timeout_seconds": 300,
        "outcome": "PASSED",
        "commands": [
            _command_entry(phase, command, index, started_at=started_at, ended_at=ended_at)
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



def _config_digest_of(commands):
    """Immutable config identity of an ordered (phase, command) sequence."""
    return command_sequence_digest(
        tuple((phase, _digest(command)) for phase, command in commands)
    )


DEFAULT_COMMANDS = (
    ("local_review_ready", "typecheck"),
    ("closure_acceptance", "acceptance"),
)
DEFAULT_CONFIG_DIGEST = _config_digest_of(DEFAULT_COMMANDS)


def _command_entry(
    phase, command, index, exit_status=0,
    started_at="2026-08-08T12:00:00+00:00",
    ended_at="2026-08-08T12:00:01+00:00",
):
    return {
        "phase": phase,
        "command_digest": _digest(command),
        "command_label": "{0}:{1}".format(phase, index),
        "started_at": started_at,
        "ended_at": ended_at,
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


class _ApprovalSignal:
    """Test-only projection of the removed store-only approval authority.

    Approval now lives in the CLI's live snapshot (T6L-F1); at the store seam
    the canonical inputs are the exact-sequence bound verification and the
    bound review set. Malformed, missing-target, or contradictory evidence
    raises :class:`MalformedEvidence` instead of a weak "unverified" signal.
    """

    UNVERIFIED = "unverified"
    APPROVED = "approved"
    STALE = "stale"


def _approval_signal(store, commit, expected_commands=None):
    """Store-level canonical approximation of approval for legacy tests."""
    if expected_commands is None:
        expected_commands = _expected_commands()
    verification = store.bound_verification(commit, expected_commands)
    reviews = store.bound_reviews(commit)
    current = store.current_commit()
    if verification is None or not reviews:
        return _ApprovalSignal.UNVERIFIED
    if current == commit:
        return _ApprovalSignal.APPROVED
    if current is None:
        return _ApprovalSignal.UNVERIFIED
    return _ApprovalSignal.STALE


def _single_verification_path(store, commit):
    """The single verification attempt path for ``commit`` (legacy helper)."""
    paths = store.verification_paths(commit)
    if len(paths) != 1:
        raise AssertionError(
            "expected exactly one verification attempt at {0}, found {1}".format(
                commit, len(paths)
            )
        )
    return paths[0]


def _write_verification(store, commit, record=None):
    """Write a PASSED verification attempt with its own config identity."""
    if record is None:
        record = _verification_record(commit=commit)
    digest = record.get("config_digest") or DEFAULT_CONFIG_DIGEST
    return store.write_verification(commit, digest, record)


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


class CommandSequenceDigestTests(StoreTestCase):
    def test_command_sequence_digest_includes_verification_timeout(self):
        commands = _expected_commands()
        slow = command_sequence_digest(commands, verification_command_timeout_seconds=3)
        fast = command_sequence_digest(commands, verification_command_timeout_seconds=4)
        self.assertNotEqual(slow, fast)

    def test_distinct_policy_activation_identities_at_one_tip_do_not_conflict(self):
        store = RunStore(self.root, "proj", 7)
        first = store.record_policy_activation(COMMIT_A, "1" * 64, "2" * 64)
        second = store.record_policy_activation(COMMIT_A, "3" * 64, "4" * 64)
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(2, len([event for event in store.read_events() if event["event_type"] == "policy_activation"]))


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
        _write_verification(store, COMMIT_A)
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
        _write_verification(store, COMMIT_A)
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
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        events = store.read_events()
        self.assertEqual([COMMIT_EVENT, VERIFICATION_EVENT, REVIEW_EVENT], [e["event_type"] for e in events])
        self.assertEqual(str(_single_verification_path(store, COMMIT_A)), events[1]["evidence_path"])
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
        events = store.read_events()
        self.assertEqual(240, events[0]["stagnation_budget_minutes"])
        self.assertEqual(1, events[0]["infra_retry_budget"])

    def test_generic_append_cannot_mint_lifecycle_events(self):
        cases = (
            (
                REVIEW_DISPATCH_EVENT,
                {"lane_id": "review-a"},
            ),
            (
                REPAIR_STRATEGY_EVENT,
                {
                    "root_cause": "missing-invariant",
                    "strategy": "structural-guard",
                    "lane_id": "fix-a",
                },
            ),
        )
        for index, (event_type, payload) in enumerate(cases, start=1):
            with self.subTest(event_type=event_type):
                store = RunStore(self.root, "proj", 40 + index)
                with self.assertRaises(StoreError):
                    store.append_event(
                        event_type,
                        COMMIT_A,
                        str(store.events_path),
                        payload=payload,
                    )
                self.assertFalse(store.events_path.exists())

    def test_typed_lifecycle_writers_preserve_valid_flow(self):
        store = RunStore(self.root, "proj", 42)

        store.record_review_dispatch(
            COMMIT_A,
            lane_id="review-a",
            current_state=ClosureState.REVIEW_READY,
        )
        store.record_repair_strategy(
            COMMIT_A,
            root_cause="missing-invariant",
            strategy="structural-guard",
            lane_id="fix-a",
            current_state=ClosureState.CHANGES_REQUIRED,
        )

        events = store.read_events()
        self.assertEqual(
            [REVIEW_DISPATCH_EVENT, REPAIR_STRATEGY_EVENT],
            [event["event_type"] for event in events],
        )

    def test_lifecycle_writers_reject_conflicting_source_states(self):
        store = RunStore(self.root, "proj", 42)

        with self.assertRaises(StoreError):
            store.record_review_dispatch(
                COMMIT_A,
                lane_id="review-a",
                current_state=ClosureState.CHANGES_REQUIRED,
            )
        with self.assertRaises(StoreError):
            store.record_repair_strategy(
                COMMIT_A,
                root_cause="missing-invariant",
                strategy="structural-guard",
                lane_id="fix-a",
                current_state=ClosureState.DESIGN_RESET,
            )

        self.assertFalse(store.events_path.exists())

    def test_repair_writer_rejects_third_strategy_even_with_forged_source_state(self):
        store = RunStore(self.root, "proj", 42)
        for commit, strategy, lane_id in (
            (COMMIT_A, "structural-guard", "fix-a"),
            (COMMIT_B, "boundary-validation", "fix-b"),
        ):
            store.record_repair_strategy(
                commit,
                root_cause="missing-invariant",
                strategy=strategy,
                lane_id=lane_id,
                current_state=ClosureState.CHANGES_REQUIRED,
            )

        with self.assertRaises(StoreError):
            store.record_repair_strategy(
                COMMIT_C,
                root_cause="missing-invariant",
                strategy="third-instance-patch",
                lane_id="fix-c",
                current_state=ClosureState.CHANGES_REQUIRED,
            )

        self.assertEqual(2, len(store.read_events()))

    def test_legacy_lifecycle_events_remain_readable(self):
        store = RunStore(self.root, "proj", 42)
        store.events_path.parent.mkdir(parents=True)
        legacy = {
            "schema_version": 1,
            "timestamp": "2026-08-08T12:00:00+00:00",
            "project": "proj",
            "pr": 42,
            "commit": COMMIT_A,
            "event_type": REPAIR_STRATEGY_EVENT,
            "evidence_path": self.durable_file("legacy-repair"),
            "root_cause": "missing-invariant",
            "strategy": "legacy-instance-patch",
        }
        store.events_path.write_text(json.dumps(legacy, sort_keys=True) + "\n")

        self.assertEqual((legacy,), store.read_events())


class AtomicRecordWriteTests(StoreTestCase):
    def test_verification_lives_at_commit_config_attempt_path(self):
        store = RunStore(self.root, "proj", 42)
        _write_verification(store, COMMIT_A)
        path = _single_verification_path(store, COMMIT_A)
        self.assertTrue(path.is_file())
        self.assertEqual("verification", path.parent.parent.parent.name)
        self.assertEqual(COMMIT_A, path.parent.parent.name)
        self.assertEqual(DEFAULT_CONFIG_DIGEST, path.parent.name)
        self.assertRegex(path.name, r"^[0-9a-f]{64}\.json$")
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
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        leftovers = []
        for directory in (store.base_dir, store.verification_dir(COMMIT_A), store.review_dir(COMMIT_A)):
            leftovers.extend(str(p) for p in directory.glob("*.tmp*"))
            leftovers.extend(str(p) for p in directory.glob(".tmp*"))
        self.assertEqual([], leftovers)

    def test_verification_is_never_silently_overwritten(self):
        store = RunStore(self.root, "proj", 42)
        record = _verification_record()
        _write_verification(store, COMMIT_A, record)
        _write_verification(store, COMMIT_A, record)
        attempt = _single_verification_path(store, COMMIT_A)
        self.assertEqual(record, json.loads(attempt.read_text()))
        verification_events = [
            e for e in store.read_events() if e["event_type"] == VERIFICATION_EVENT
        ]
        self.assertEqual(1, len(verification_events))
        # A different-byte attempt appends under the same config identity and
        # never overwrites the original record bytes (T6L-F2 append-only).
        _write_verification(
            store, COMMIT_A,
            _verification_record(started_at="2026-08-08T13:00:00+00:00"),
        )
        self.assertEqual(2, len(store.verification_paths(COMMIT_A)))
        self.assertEqual(record, json.loads(attempt.read_text()))


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

    def test_rejects_missing_tmp_session_path(self):
        store = RunStore(self.root, "proj", 42)
        missing = os.path.join(
            tempfile.gettempdir(),
            "missing-harness-session-{0}".format(os.getpid()),
            "evidence.json",
        )
        with self.assertRaises(ForbiddenEvidencePath):
            store.record_commit(COMMIT_A, missing)

    def test_rejects_missing_claude_jobs_session_path(self):
        store = RunStore(self.root, "proj", 42)
        missing = os.path.join(
            os.path.expanduser("~/.claude/jobs"),
            "missing-session-{0}".format(os.getpid()),
            "evidence.json",
        )
        with self.assertRaises(ForbiddenEvidencePath):
            store.record_commit(COMMIT_A, missing)

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
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(_ApprovalSignal.APPROVED, _approval_signal(store, COMMIT_A))
        store.record_commit(COMMIT_B, self.durable_file("tip-b"))
        self.assertEqual(COMMIT_B, store.current_commit())
        self.assertEqual(_ApprovalSignal.STALE, _approval_signal(store, COMMIT_A))
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_B))

    def test_approval_requires_both_verification_and_review(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))
        _write_verification(store, COMMIT_A)
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(_ApprovalSignal.APPROVED, _approval_signal(store, COMMIT_A))

    def test_unknown_commit_without_any_evidence_is_unverified(self):
        store = RunStore(self.root, "proj", 42)
        self.assertIsNone(store.current_commit())
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))


class MissingArtifactFailsClosedTests(StoreTestCase):
    def test_missing_referenced_artifact_never_returns_favorable_state(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))
        _write_verification(store, COMMIT_A)
        self.assertEqual(_ApprovalSignal.APPROVED, _approval_signal(store, COMMIT_A))
        _single_verification_path(store, COMMIT_A).unlink()
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)
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
            _approval_signal(store, COMMIT_A)

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
        _write_verification(store, COMMIT_A)
        attempt = _single_verification_path(store, COMMIT_A)
        attempt.write_text("{broken")
        with self.assertRaises(MalformedEvidence):
            store.read_verification(COMMIT_A, DEFAULT_CONFIG_DIGEST, attempt.stem)
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_non_object_verification_artifact_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        _write_verification(store, COMMIT_A)
        attempt = _single_verification_path(store, COMMIT_A)
        attempt.write_text("[1, 2]\n")
        with self.assertRaises(MalformedEvidence):
            store.read_verification(COMMIT_A, DEFAULT_CONFIG_DIGEST, attempt.stem)
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_malformed_review_artifact_raises(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.write_review(COMMIT_A, "review-1", _review_record())
        store.review_path(COMMIT_A, "review-1").write_text("42")
        with self.assertRaises(MalformedEvidence):
            store.read_review(COMMIT_A, "review-1")
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_contradictory_record_commits_are_rejected(self):
        store = RunStore(self.root, "proj", 42)
        with self.assertRaises(MalformedEvidence):
            _write_verification(store, COMMIT_A, _verification_record(COMMIT_B))
        with self.assertRaises(MalformedEvidence):
            store.write_review(COMMIT_A, "review-1", _review_record(COMMIT_B))

    def test_malformed_commit_is_rejected(self):
        store = RunStore(self.root, "proj", 42)
        for bad in ("", "   ", "a" * 39, "A" * 40, "not-a-commit", None, 42):
            with self.subTest(commit=bad):
                with self.assertRaises((StoreError, TypeError)):
                    store.record_commit(bad, self.durable_file("x"))
                with self.assertRaises((StoreError, TypeError)):
                    store.bound_verification(bad, _expected_commands())


class BoundArtifactSeamTests(StoreTestCase):
    """C6C-F4: only event-bound durable artifacts are authority."""

    def test_bound_verification_absent_returns_none(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands()))

    def test_bound_verification_returns_event_bound_record(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        self.assertEqual(_verification_record(), store.bound_verification(COMMIT_A, _expected_commands()))

    def test_passed_record_rejects_timed_out_command_even_with_zero_exit(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        record = _verification_record()
        record["commands"][0]["timed_out"] = True
        record["commands"][0]["failure_reason"] = "timeout"
        _write_verification(store, COMMIT_A, record)

        with self.assertRaisesRegex(MalformedEvidence, "timed out"):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_legacy_verification_without_timeout_is_not_selected_for_new_timeout(self):
        store = RunStore(self.root, "proj", 42)
        commands = _expected_commands()
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        legacy_digest = legacy_command_sequence_digest(commands)
        legacy_record = dict(_verification_record(config_digest=legacy_digest))
        legacy_record.pop("verification_command_timeout_seconds", None)
        store.write_verification(COMMIT_A, legacy_digest, legacy_record)
        self.assertIsNone(store.bound_verification(COMMIT_A, commands, expected_verification_command_timeout_seconds=5))

    def test_new_verification_without_timeout_is_rejected_as_malformed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        commands = _expected_commands()
        new_digest = command_sequence_digest(commands, verification_command_timeout_seconds=5)
        new_record = dict(_verification_record(config_digest=new_digest))
        new_record.pop("verification_command_timeout_seconds", None)
        store.write_verification(COMMIT_A, new_digest, new_record)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, commands, expected_verification_command_timeout_seconds=5)

    def test_orphan_verification_artifact_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        self._write_artifact(
            store.verification_dir(COMMIT_A) / "orphan-config" / "orphan.json",
            _verification_record(),
        )
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
        self._write_artifact(
            store.verification_dir(COMMIT_A) / "orphan-config" / "orphan.json",
            _verification_record(),
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_missing_artifact_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        _single_verification_path(store, COMMIT_A).unlink()
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_symlink_replacement_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        target = _single_verification_path(store, COMMIT_A)
        decoy = self.root / "decoy.json"
        decoy.write_text(json.dumps(_verification_record()))
        target.unlink()
        target.symlink_to(decoy)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_replaced_record_with_contradictory_commit_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        target = _single_verification_path(store, COMMIT_A)
        replaced = dict(_verification_record(COMMIT_A), commit=COMMIT_B)
        target.write_text(_serialize(replaced).decode())
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_failed_outcome_artifact_is_never_authority(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        target = _single_verification_path(store, COMMIT_A)
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
                mutated["config_digest"] = command_sequence_digest(
                    tuple(
                        (entry["phase"], entry.get("command_digest") or ("0" * 64))
                        for entry in commands
                    )
                )
                _write_verification(store, COMMIT_A, mutated)
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
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(0o600, _single_verification_path(store, COMMIT_A).stat().st_mode & 0o777)
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
                        "STAGNATION_CONFIG",
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
            ("event_type", "STAGNATION_CONFIG"),
        )
        for key, value in identical:
            with self.subTest(key=key):
                with self.assertRaises(MalformedEvidence):
                    store.append_event(
                        "STAGNATION_CONFIG",
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
            "STAGNATION_CONFIG",
            COMMIT_A,
            self.durable_file("tip"),
            payload={"root_cause": "x"},
        )
        event = store.read_events()[0]
        self.assertEqual("STAGNATION_CONFIG", event["event_type"])
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
            _approval_signal(store, COMMIT_A)

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
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertIsNone(store.current_commit())
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))

    def test_approval_is_approved_once_the_tip_is_observed(self):
        store = RunStore(self.root, "proj", 42)
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self.assertEqual(_ApprovalSignal.APPROVED, _approval_signal(store, COMMIT_A))

    def test_missing_current_tip_evidence_never_approves(self):
        store = RunStore(self.root, "proj", 42)
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertIsNone(store.current_commit())
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_B))

    def test_stale_requires_valid_artifacts_for_the_queried_commit(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.record_commit(COMMIT_B, self.durable_file("tip-b"))
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))
        _write_verification(store, COMMIT_A)
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(_ApprovalSignal.STALE, _approval_signal(store, COMMIT_A))


class OrphanEvidenceFailsClosedTests(StoreTestCase):
    """T3R-F1: a failed event append must not leave approval evidence."""

    def test_failed_event_append_raises_and_rolls_back_the_artifact(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        os.chmod(store.events_path, 0o400)
        try:
            with self.assertRaises(OSError):
                _write_verification(store, COMMIT_A)
            self.assertEqual((), store.verification_paths(COMMIT_A))
            self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))
        finally:
            os.chmod(store.events_path, 0o600)

    def test_failed_append_then_restored_retry_reaches_approved(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        os.chmod(store.events_path, 0o400)
        try:
            with self.assertRaises(OSError):
                _write_verification(store, COMMIT_A)
        finally:
            os.chmod(store.events_path, 0o600)
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(
            [COMMIT_EVENT, VERIFICATION_EVENT, REVIEW_EVENT],
            [e["event_type"] for e in store.read_events()],
        )
        self.assertEqual(_ApprovalSignal.APPROVED, _approval_signal(store, COMMIT_A))

    def test_orphan_artifacts_without_matching_events_never_approve(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self._write_artifact(
            store.verification_dir(COMMIT_A) / "orphan-config" / "orphan.json",
            _verification_record(),
        )
        self._write_artifact(store.review_path(COMMIT_A, "review-1"), _review_record())
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_failed_repair_append_leaves_an_orphan_that_cannot_approve(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        self._write_artifact(
            store.verification_path(COMMIT_A, DEFAULT_CONFIG_DIGEST, "f" * 64),
            _verification_record(),
        )
        os.chmod(store.events_path, 0o400)
        try:
            with self.assertRaises(OSError):
                _write_verification(store, COMMIT_A)
            self.assertTrue(store.verification_paths(COMMIT_A))
            with self.assertRaises(MalformedEvidence):
                _approval_signal(store, COMMIT_A)
        finally:
            os.chmod(store.events_path, 0o600)

    def test_identical_retry_repairs_a_missing_event(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        attempt_id = hashlib.sha256(_serialize(_verification_record())).hexdigest()
        self._write_artifact(
            store.verification_path(COMMIT_A, DEFAULT_CONFIG_DIGEST, attempt_id),
            _verification_record(),
        )
        _write_verification(store, COMMIT_A)
        self.assertEqual(
            [COMMIT_EVENT, VERIFICATION_EVENT], [e["event_type"] for e in store.read_events()]
        )
        _write_verification(store, COMMIT_A)
        self.assertEqual(2, len(store.read_events()))

    def test_already_complete_identical_write_adds_no_duplicate_event(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        _write_verification(store, COMMIT_A)
        _write_verification(store, COMMIT_A)
        self.assertEqual(
            [COMMIT_EVENT, VERIFICATION_EVENT], [e["event_type"] for e in store.read_events()]
        )

    def test_write_rejects_verification_dir_symlinked_into_tmp_before_any_write(self):
        session = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, session, ignore_errors=True)
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        store.verification_dir(COMMIT_A).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(session, store.verification_dir(COMMIT_A))
        with self.assertRaises(ForbiddenEvidencePath):
            _write_verification(store, COMMIT_A)
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
        self._write_artifact(
            store.verification_dir(COMMIT_A) / "orphan-config" / "orphan.json",
            _verification_record(),
        )
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
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_approval_requires_a_matching_review_event(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        _write_verification(store, COMMIT_A)
        self._write_artifact(store.review_path(COMMIT_A, "review-1"), _review_record())
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)
        store.write_review(COMMIT_A, "review-1", _review_record())
        self.assertEqual(_ApprovalSignal.APPROVED, _approval_signal(store, COMMIT_A))


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
        _write_verification(store, COMMIT_A)
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
        self.assertEqual(_ApprovalSignal.APPROVED, _approval_signal(store, COMMIT_A))

    def test_concurrent_different_verification_bytes_coexist_as_attempts(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip-a"))
        original = _verification_record()
        changed = dict(
            original,
            commands=[_command_entry("local_review_ready", "pytest", 1)],
        )
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(record):
            barrier.wait()
            try:
                _write_verification(store, COMMIT_A, record)
                outcomes.append("ok")
            except EvidenceConflict:
                outcomes.append("conflict")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, (original, changed)))
        self.assertEqual(2, outcomes.count("ok"), outcomes)
        self.assertEqual(0, outcomes.count("conflict"), outcomes)
        stored = [
            json.loads(path.read_text()) for path in store.verification_paths(COMMIT_A)
        ]
        self.assertEqual(
            {_serialize(original), _serialize(changed)},
            {_serialize(record) for record in stored},
        )

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
        _write_verification(store, COMMIT_A, _new_verification_record(commands=commands))
        return store

    def test_exact_current_sequence_is_green(self):
        store = self._green()
        record = store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual("PASSED", record["outcome"])

    def test_omitted_command_fails_closed(self):
        store = self._green(commands=(("local_review_ready", "typecheck"),))
        self.assertIsNone(store.bound_verification(COMMIT_A, _expected_commands()))
        self.assertEqual(_ApprovalSignal.UNVERIFIED, _approval_signal(store, COMMIT_A))

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
        _write_verification(store, COMMIT_A, record)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_boolean_status_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        record = _new_verification_record()
        record["commands"][0]["exit_status"] = True
        _write_verification(store, COMMIT_A, record)
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
        _write_verification(store, COMMIT_A)
        event = store.read_events()[-1]
        digest = event["content_digest"]
        self.assertEqual(64, len(digest))
        self.assertRegex(digest, "^[0-9a-f]{64}$")
        expected = hashlib.sha256(
            _single_verification_path(store, COMMIT_A).read_bytes()
        ).hexdigest()
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
        _write_verification(store, COMMIT_A)
        replaced = _new_verification_record()
        replaced["commands"][0]["command_label"] = "local_review_ready:9"
        _single_verification_path(store, COMMIT_A).write_text(_serialize(replaced).decode())
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_same_path_review_replacement_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_review(COMMIT_A, "r1", _review_record())
        replaced = dict(_review_record(), verdict="CHANGES_REQUIRED")
        store.review_path(COMMIT_A, "r1").write_text(_serialize(replaced).decode())
        with self.assertRaises(MalformedEvidence):
            store.bound_reviews(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_legacy_path_only_verification_event_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        events = list(store.read_events())
        events[-1].pop("content_digest")
        payload = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        store.events_path.write_text(payload)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_malformed_digest_event_fails_closed(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        events = list(store.read_events())
        events[-1]["content_digest"] = "NOT_HEX_DIGEST"
        payload = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        store.events_path.write_text(payload)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_wrong_digest_event_never_binds_authority(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        events = list(store.read_events())
        events[-1] = {
            "schema_version": 1,
            "timestamp": "2026-08-08T12:00:00+00:00",
            "project": "proj",
            "pr": 42,
            "commit": COMMIT_A,
            "event_type": VERIFICATION_EVENT,
            "evidence_path": str(_single_verification_path(store, COMMIT_A)),
            "content_digest": _digest("forged bytes"),
        }
        payload = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        store.events_path.write_text(payload)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())
        with self.assertRaises(MalformedEvidence):
            _approval_signal(store, COMMIT_A)

    def test_identical_retry_remains_idempotent_with_digests(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        _write_verification(store, COMMIT_A)
        _write_verification(store, COMMIT_A)
        store.write_review(COMMIT_A, "r1", _review_record())
        events = [e for e in store.read_events() if e["event_type"] == VERIFICATION_EVENT]
        self.assertEqual(1, len(events))
        record = store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual("PASSED", record["outcome"])
        self.assertEqual(_ApprovalSignal.APPROVED, _approval_signal(store, COMMIT_A))

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
                "started_at": "2026-08-08T12:00:00+00:00",
                "ended_at": "2026-08-08T12:00:01+00:00",
            },
        )
        event = store.read_events()[-1]
        self.assertEqual("FAILED", event["outcome"])
        self.assertNotIn("content_digest", event)
        self.assertEqual(_digest("boom"), event["failed_command_digest"])



class NoParallelApprovalAuthorityTests(StoreTestCase):
    """T6L-F1: no weak store-only approval authority remains on the surface."""

    def test_approval_status_helper_is_removed(self):
        self.assertFalse(hasattr(RunStore, "approval_status"))

    def test_approval_state_enum_is_removed(self):
        import pr_closure.store as store_module

        self.assertFalse(hasattr(store_module, "ApprovalState"))

    def test_approval_status_is_not_reachable_through_any_public_alias(self):
        import pr_closure.store as store_module

        public_names = {
            name
            for name in dir(store_module)
            if not name.startswith("_") and "approval" in name.casefold()
        }
        self.assertEqual(set(), public_names)


class SameTipReverificationStoreTests(StoreTestCase):
    """T6L-F2: append-only attempt records with immutable config identity."""

    def _store(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        return store

    def test_first_pass_lands_under_commit_and_config_digest(self):
        store = self._store()
        store.write_verification(COMMIT_A, DEFAULT_CONFIG_DIGEST, _verification_record())
        paths = store.verification_paths(COMMIT_A)
        self.assertEqual(1, len(paths))
        self.assertEqual(DEFAULT_CONFIG_DIGEST, paths[0].parent.name)
        self.assertEqual(COMMIT_A, paths[0].parent.parent.name)
        record = store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual("PASSED", record["outcome"])
        self.assertEqual(DEFAULT_CONFIG_DIGEST, record["config_digest"])

    def test_same_config_rerun_coexists_as_a_distinct_attempt(self):
        store = self._store()
        store.write_verification(
            COMMIT_A, DEFAULT_CONFIG_DIGEST,
            _verification_record(started_at="2026-08-08T12:00:00+00:00"),
        )
        store.write_verification(
            COMMIT_A, DEFAULT_CONFIG_DIGEST,
            _verification_record(started_at="2026-08-08T13:00:00+00:00"),
        )
        self.assertEqual(2, len(store.verification_paths(COMMIT_A)))
        events = [e for e in store.read_events() if e["event_type"] == VERIFICATION_EVENT]
        self.assertEqual(2, len(events))
        record = store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual("PASSED", record["outcome"])
        self.assertEqual("2026-08-08T13:00:00+00:00", record["commands"][0]["started_at"])

    def test_changed_config_rerun_coexists_under_a_new_identity(self):
        store = self._store()
        old_commands = DEFAULT_COMMANDS
        new_commands = (
            ("local_review_ready", "typecheck --strict"),
            ("closure_acceptance", "acceptance"),
        )
        old_digest = _config_digest_of(old_commands)
        new_digest = _config_digest_of(new_commands)
        store.write_verification(
            COMMIT_A, old_digest, _verification_record(config_digest=old_digest, commands=old_commands)
        )
        store.write_verification(
            COMMIT_A, new_digest, _verification_record(config_digest=new_digest, commands=new_commands)
        )
        self.assertEqual(2, len(store.verification_paths(COMMIT_A)))
        self.assertEqual(
            old_digest,
            store.bound_verification(COMMIT_A, _expected_commands(old_commands))["config_digest"],
        )
        self.assertEqual(
            new_digest,
            store.bound_verification(COMMIT_A, _expected_commands(new_commands))["config_digest"],
        )

    def test_later_failed_attempt_never_supersedes_an_earlier_pass(self):
        store = self._store()
        store.write_verification(COMMIT_A, DEFAULT_CONFIG_DIGEST, _verification_record())
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(store.events_path),
            payload={
                "outcome": "FAILED",
                "started_at": "2026-08-08T14:00:00+00:00",
                "ended_at": "2026-08-08T14:00:01+00:00",
                "commands": [_command_entry("local_review_ready", "boom", 1, exit_status=9)],
                "failed_command_digest": _digest("boom"),
            },
        )
        record = store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual("PASSED", record["outcome"])

    def test_failed_attempt_then_pass_returns_the_pass(self):
        store = self._store()
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(store.events_path),
            payload={
                "outcome": "FAILED",
                "started_at": "2026-08-08T14:00:00+00:00",
                "ended_at": "2026-08-08T14:00:01+00:00",
                "commands": [_command_entry("local_review_ready", "boom", 1, exit_status=9)],
                "failed_command_digest": _digest("boom"),
            },
        )
        store.write_verification(COMMIT_A, DEFAULT_CONFIG_DIGEST, _verification_record())
        record = store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual("PASSED", record["outcome"])

    def test_append_only_across_attempts_and_configs(self):
        store = self._store()
        store.write_verification(COMMIT_A, DEFAULT_CONFIG_DIGEST, _verification_record())
        original = {
            path: path.read_bytes() for path in store.verification_paths(COMMIT_A)
        }
        other_digest = _config_digest_of((("local_review_ready", "other"), ("closure_acceptance", "acceptance")))
        store.write_verification(
            COMMIT_A, DEFAULT_CONFIG_DIGEST,
            _verification_record(started_at="2026-08-08T13:00:00+00:00"),
        )
        store.write_verification(
            COMMIT_A, other_digest,
            _verification_record(
                config_digest=other_digest,
                commands=(("local_review_ready", "other"), ("closure_acceptance", "acceptance")),
            ),
        )
        for path, bytes_ in original.items():
            self.assertEqual(bytes_, path.read_bytes(), path)


class VerificationTreeShapeTests(StoreTestCase):
    """T6L-F6: verification-tree enumeration is strict and fail-closed.

    Every entry under ``verification/<commit>/`` must conform to the exact
    ``<config-digest>/<attempt-id>.json`` grammar. Legacy flat files,
    malformed config-directory names, non-directory entries where a config
    directory is required, non-attempt children inside a valid config
    directory, and nested/symlink shapes all raise :class:`MalformedEvidence`
    instead of being silently skipped. Valid multiple config/attempt trees
    remain accepted.
    """

    def _store(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        return store

    def test_direct_json_file_under_commit_dir_fails_closed(self):
        store = self._store()
        store.verification_dir(COMMIT_A).mkdir(parents=True, exist_ok=True)
        (store.verification_dir(COMMIT_A) / "orphan.json").write_bytes(
            _serialize(_verification_record())
        )
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_absent_commit_root_returns_empty(self):
        store = self._store()
        self.assertEqual((), store.verification_paths(COMMIT_A))
        self.assertFalse(store.verification_exists(COMMIT_A))

    def test_regular_file_commit_root_fails_closed(self):
        store = self._store()
        target = store.verification_dir(COMMIT_A)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not a directory")
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            store.verification_exists(COMMIT_A)

    def test_symlink_commit_root_fails_closed(self):
        for label, kind in (("to-directory", "directory"), ("to-file", "file")):
            with self.subTest(link=label):
                store = self._store()
                root = store.verification_dir(COMMIT_A)
                root.parent.mkdir(parents=True, exist_ok=True)
                if kind == "directory":
                    real = self.root / "real-verification-dir"
                    real.mkdir(exist_ok=True)
                else:
                    real = self.root / "real-verification-file"
                    real.write_text("x")
                os.symlink(real, root)
                try:
                    with self.assertRaises(MalformedEvidence):
                        store.verification_paths(COMMIT_A)
                finally:
                    root.unlink()

    def test_fifo_commit_root_fails_closed(self):
        store = self._store()
        target = store.verification_dir(COMMIT_A)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(target)
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)

    def test_real_directory_commit_root_remains_accepted(self):
        store = self._store()
        _write_verification(store, COMMIT_A)
        self.assertEqual(1, len(store.verification_paths(COMMIT_A)))

    def test_absent_verification_parent_returns_empty(self):
        store = self._store()
        self.assertFalse((store.base_dir / "verification").exists())
        self.assertEqual((), store.verification_paths(COMMIT_A))
        self.assertFalse(store.verification_exists(COMMIT_A))

    def test_regular_file_verification_parent_fails_closed(self):
        store = self._store()
        parent = store.base_dir / "verification"
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_text("not a directory")
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            store.verification_exists(COMMIT_A)

    def test_dangling_symlink_verification_parent_fails_closed(self):
        store = self._store()
        parent = store.base_dir / "verification"
        parent.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(self.root / "no-such-verification-target", parent)
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            store.verification_exists(COMMIT_A)

    def test_external_directory_symlink_verification_parent_fails_closed(self):
        store = self._store()
        parent = store.base_dir / "verification"
        parent.parent.mkdir(parents=True, exist_ok=True)
        real = self.root / "external-verification-directory"
        real.mkdir(exist_ok=True)
        os.symlink(real, parent)
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            store.verification_exists(COMMIT_A)

    def test_external_file_symlink_verification_parent_fails_closed(self):
        store = self._store()
        parent = store.base_dir / "verification"
        parent.parent.mkdir(parents=True, exist_ok=True)
        real = self.root / "external-verification-file"
        real.write_text("x")
        os.symlink(real, parent)
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            store.verification_exists(COMMIT_A)

    def test_fifo_verification_parent_fails_closed(self):
        store = self._store()
        parent = store.base_dir / "verification"
        parent.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(parent)
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)
        with self.assertRaises(MalformedEvidence):
            store.verification_exists(COMMIT_A)

    def test_non_directory_entry_where_config_dir_is_required_fails_closed(self):
        store = self._store()
        store.verification_dir(COMMIT_A).mkdir(parents=True, exist_ok=True)
        (store.verification_dir(COMMIT_A) / ("a" * 64)).write_bytes(
            _serialize(_verification_record())
        )
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)

        store = self._store()
        store.verification_dir(COMMIT_A).mkdir(parents=True, exist_ok=True)
        session = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, session, ignore_errors=True)
        os.symlink(session, store.verification_dir(COMMIT_A) / ("b" * 64))
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)

    def test_config_dir_name_must_be_exact_lowercase_64_hex(self):
        for bad in (
            "orphan-config",
            "ABCDEF",
            "a" * 63,
            "a" * 65,
            ("a" * 63) + "Z",
            "a" * 64 + "-",
        ):
            with self.subTest(name=bad):
                store = self._store()
                (store.verification_dir(COMMIT_A) / bad).mkdir(parents=True, exist_ok=True)
                with self.assertRaises(MalformedEvidence):
                    store.verification_paths(COMMIT_A)

    def test_non_attempt_child_inside_valid_config_dir_fails_closed(self):
        for name in (
            "notes.txt",
            "attempt.json",
            "orphan.json",
            ("f" * 64) + ".txt",
            ("f" * 64) + ".JSON",
            ("f" * 64) + ".json.bak",
        ):
            with self.subTest(name=name):
                store = self._store()
                config_dir = store.verification_dir(COMMIT_A) / DEFAULT_CONFIG_DIGEST
                config_dir.mkdir(parents=True, exist_ok=True)
                (config_dir / name).write_bytes(_serialize(_verification_record()))
                with self.assertRaises(MalformedEvidence):
                    store.verification_paths(COMMIT_A)

    def test_nested_directory_and_symlink_shape_under_valid_config_dir_fails_closed(self):
        store = self._store()
        config_dir = store.verification_dir(COMMIT_A) / DEFAULT_CONFIG_DIGEST
        config_dir.mkdir(parents=True, exist_ok=True)
        nested = config_dir / "nested"
        nested.mkdir(exist_ok=True)
        (nested / ("f" * 64 + ".json")).write_bytes(_serialize(_verification_record()))
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)

        store = self._store()
        config_dir = store.verification_dir(COMMIT_A) / DEFAULT_CONFIG_DIGEST
        config_dir.mkdir(parents=True, exist_ok=True)
        decoy = self.root / "decoy.json"
        decoy.write_bytes(_serialize(_verification_record()))
        (config_dir / ("f" * 64 + ".json")).symlink_to(decoy)
        with self.assertRaises(MalformedEvidence):
            store.verification_paths(COMMIT_A)

    def test_valid_multiple_configs_and_attempts_remain_accepted(self):
        store = self._store()
        old_commands = DEFAULT_COMMANDS
        new_commands = (
            ("local_review_ready", "typecheck --strict"),
            ("closure_acceptance", "acceptance"),
        )
        old_digest = _config_digest_of(old_commands)
        new_digest = _config_digest_of(new_commands)
        store.write_verification(
            COMMIT_A, old_digest,
            _verification_record(config_digest=old_digest, commands=old_commands),
        )
        store.write_verification(
            COMMIT_A, old_digest,
            _verification_record(
                config_digest=old_digest,
                commands=old_commands,
                started_at="2026-08-08T13:00:00+00:00",
            ),
        )
        store.write_verification(
            COMMIT_A, new_digest,
            _verification_record(config_digest=new_digest, commands=new_commands),
        )
        paths = store.verification_paths(COMMIT_A)
        self.assertEqual(3, len(paths))
        self.assertEqual({old_digest, new_digest}, {p.parent.name for p in paths})
        self.assertEqual(3, len({p.name for p in paths}))
        self.assertEqual(
            old_digest,
            store.bound_verification(COMMIT_A, _expected_commands(old_commands))["config_digest"],
        )
        self.assertEqual(
            new_digest,
            store.bound_verification(COMMIT_A, _expected_commands(new_commands))["config_digest"],
        )


class PinnedArtifactReadTests(StoreTestCase):
    """T6L-F4: descriptor-pinned no-follow reads with identity re-check."""

    def _bound_store(self, record=None):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, DEFAULT_CONFIG_DIGEST, record or _verification_record())
        paths = store.verification_paths(COMMIT_A)
        self.assertEqual(1, len(paths))
        return store, paths[0]

    def _fd_count(self):
        return len(os.listdir("/proc/self/fd"))

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "requires /proc/self/fd")
    def test_every_descriptor_is_closed(self):
        store, _ = self._bound_store()
        before = self._fd_count()
        for _ in range(25):
            store.bound_verification(COMMIT_A, _expected_commands())
        self.assertEqual(before, self._fd_count())

    def test_same_path_swap_after_read_is_rejected(self):
        store, target = self._bound_store()
        decoy = self.root / "decoy.json"
        decoy.write_bytes(_serialize(_verification_record(commit=COMMIT_B)))
        original_lstat = os.lstat

        def swapped(path):
            if os.fspath(path) == os.fspath(target):
                return original_lstat(os.fspath(decoy))
            return original_lstat(path)

        import unittest.mock as mock

        with mock.patch("pr_closure.store.os.lstat", side_effect=swapped):
            with self.assertRaises(MalformedEvidence):
                store.bound_verification(COMMIT_A, _expected_commands())

    def test_content_swap_during_read_is_rejected(self):
        store, target = self._bound_store()
        replacement = _serialize(_verification_record(commit=COMMIT_B))
        original_read = os.read

        class _SwapOnce:
            def __init__(self):
                self.calls = 0

            def __call__(self, fd, size):
                self.calls += 1
                if self.calls == 1:
                    return replacement
                return b""

        import unittest.mock as mock

        with mock.patch("pr_closure.store.os.read", side_effect=_SwapOnce()):
            with self.assertRaises(MalformedEvidence):
                store.bound_verification(COMMIT_A, _expected_commands())

    def test_symlink_target_is_rejected_by_no_follow_open(self):
        store, target = self._bound_store()
        decoy = self.root / "decoy-link.json"
        decoy.write_bytes(_serialize(_verification_record()))
        target.unlink()
        os.symlink(decoy, target)
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_multi_link_regular_file_is_rejected(self):
        store, target = self._bound_store()
        hard = self.root / "hard.json"
        os.link(target, hard)
        try:
            with self.assertRaises(MalformedEvidence):
                store.bound_verification(COMMIT_A, _expected_commands())
        finally:
            os.unlink(hard)


class EventArtifactRelationTests(StoreTestCase):
    """T6L-F5: the complete event/artifact relation is validated both ways."""

    def _approved(self):
        store = RunStore(self.root, "proj", 42)
        store.record_commit(COMMIT_A, self.durable_file("tip"))
        store.write_verification(COMMIT_A, DEFAULT_CONFIG_DIGEST, _verification_record())
        store.write_review(COMMIT_A, "r1", _review_record())
        return store

    def test_review_event_with_missing_target_fails_closed(self):
        store = self._approved()
        store.append_event(
            REVIEW_EVENT,
            COMMIT_A,
            str(store.review_path(COMMIT_A, "ghost")),
            content_digest=_digest(b"ghost bytes"),
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_reviews(COMMIT_A)

    def test_verification_event_with_missing_target_fails_closed(self):
        store = self._approved()
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(store.verification_path(COMMIT_A, DEFAULT_CONFIG_DIGEST, "f" * 64)),
            content_digest=_digest(b"ghost bytes"),
            config_digest=DEFAULT_CONFIG_DIGEST,
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_conflicting_digest_event_for_the_same_path_fails_closed(self):
        store = self._approved()
        target = store.verification_paths(COMMIT_A)[0]
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(target),
            content_digest=_digest(b"conflicting bytes"),
            config_digest=DEFAULT_CONFIG_DIGEST,
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_conflicting_review_event_for_the_same_path_fails_closed(self):
        store = self._approved()
        target = store.review_path(COMMIT_A, "r1")
        store.append_event(
            REVIEW_EVENT,
            COMMIT_A,
            str(target),
            content_digest=_digest(b"conflicting review bytes"),
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_reviews(COMMIT_A)

    def test_legacy_path_only_verification_event_fails_closed(self):
        store = self._approved()
        events = list(store.read_events())
        verification = next(
            e for e in events
            if e["event_type"] == VERIFICATION_EVENT and "content_digest" in e
        )
        verification.pop("content_digest")
        verification.pop("config_digest")
        store.events_path.write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_failed_verification_event_requires_strict_shape(self):
        store = self._approved()
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(store.events_path),
            payload={"outcome": "FAILED", "failed_command_digest": _digest("boom")},
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_failed_verification_event_must_bind_the_events_file(self):
        store = self._approved()
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            self.durable_file("elsewhere"),
            payload={
                "outcome": "FAILED",
                "commands": [_command_entry("local_review_ready", "boom", 1, exit_status=9)],
                "failed_command_digest": _digest("boom"),
            },
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_failed_verification_event_digest_must_name_a_command(self):
        store = self._approved()
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(store.events_path),
            payload={
                "outcome": "FAILED",
                "commands": [_command_entry("local_review_ready", "boom", 1, exit_status=9)],
                "failed_command_digest": _digest("not-in-commands"),
            },
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())

    def test_valid_plus_conflicting_relation_still_fails_closed(self):
        store = self._approved()
        target = store.verification_paths(COMMIT_A)[0]
        store.append_event(
            VERIFICATION_EVENT,
            COMMIT_A,
            str(target),
            content_digest=_digest(b"other bytes"),
            config_digest=DEFAULT_CONFIG_DIGEST,
        )
        with self.assertRaises(MalformedEvidence):
            store.bound_verification(COMMIT_A, _expected_commands())


if __name__ == "__main__":
    unittest.main()
