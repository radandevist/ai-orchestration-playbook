import fcntl
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pr_closure import lease as lease_module
from pr_closure.lease import (
    LEASE_SCHEMA_VERSION,
    ForbiddenLeaseRoot,
    HeavyJobLease,
    LaneIdentityError,
    LaneRegressionError,
    LaneSnapshot,
    LaneSnapshotError,
    LeaseError,
    LeaseMetadata,
    LeaseMetadataError,
    LeaseOwnershipError,
    LeasePathEscape,
    LeaseUnavailable,
    is_lane_live,
    validate_lane_snapshot,
    validate_lease_metadata,
)

CACHE_BASE = Path.home() / ".cache" / "pr-closure-lease-test"

DEFAULT_ARGV = ("bash", "-c", "echo heavy-job")


class LeaseTestCase(unittest.TestCase):
    def setUp(self):
        CACHE_BASE.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=CACHE_BASE))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def new_lease(self, project="proj", pr=7, argv=DEFAULT_ARGV):
        return HeavyJobLease(self.root, project, pr, argv)

    def meta_path(self, project="proj", pr=7):
        return self.root / project / str(pr) / "heavy-job.meta.json"

    def lock_path(self, project="proj", pr=7):
        return self.root / project / str(pr) / "heavy-job.lock"

    def fd_count(self):
        fd_dir = "/proc/self/fd"
        if not os.path.isdir(fd_dir):
            self.skipTest("procfs not available")
        return len(os.listdir(fd_dir))

    def run_bounded_probe(self, script, timeout=8):
        """Run ``script`` in a subprocess, failing the test if it hangs."""
        env = dict(os.environ)
        tools_dir = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = tools_dir + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        try:
            return subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.fail(
                "subprocess probe hung past the deadline; a metadata entry "
                "blocked instead of returning quickly"
            )


class LeasePathTests(LeaseTestCase):
    def test_rejects_relative_root(self):
        with self.assertRaises(ForbiddenLeaseRoot):
            HeavyJobLease("relative/root", "proj", 1, DEFAULT_ARGV)

    def test_rejects_temporary_session_roots(self):
        for root in (Path("/tmp/lease-root"), Path(os.path.expanduser("~/.claude/jobs/lease"))):
            with self.subTest(root=root):
                with self.assertRaises(ForbiddenLeaseRoot):
                    HeavyJobLease(root, "proj", 1, DEFAULT_ARGV)

    def test_rejects_root_through_a_symlink_escape(self):
        link = self.root / "escaped-root"
        os.symlink("/tmp", link)
        with self.assertRaises(ForbiddenLeaseRoot):
            HeavyJobLease(link, "proj", 1, DEFAULT_ARGV)

    def test_rejects_escaped_project_components(self):
        for bad in ("../etc", "a/b", "a\\b", "..", ".", "", "x\x00", " proj"):
            with self.subTest(project=bad):
                with self.assertRaises(LeasePathEscape):
                    HeavyJobLease(self.root, bad, 1, DEFAULT_ARGV)

    def test_rejects_non_positive_pr(self):
        for bad in (0, -1, 1.5, "7", True, None, False):
            with self.subTest(pr=bad):
                with self.assertRaises(LeasePathEscape):
                    HeavyJobLease(self.root, "proj", bad, DEFAULT_ARGV)

    def test_safe_project_and_pr_build_a_bounded_path(self):
        lease = self.new_lease()
        self.assertEqual(self.lock_path(), lease.lock_path)
        self.assertEqual(self.meta_path(), lease.meta_path)


class HeavyJobLeaseTests(LeaseTestCase):
    def test_first_process_acquires_and_writes_metadata(self):
        lease = self.new_lease()
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertTrue(lease.is_held)
        self.assertTrue(self.lock_path().exists())
        metadata = lease.metadata
        self.assertIsInstance(metadata, LeaseMetadata)
        self.assertEqual(LEASE_SCHEMA_VERSION, metadata.schema_version)
        self.assertEqual(os.getpid(), metadata.pid)
        self.assertEqual(DEFAULT_ARGV, metadata.command_argv)
        self.assertEqual("proj", metadata.project)
        self.assertEqual(7, metadata.pr)
        started = datetime.fromisoformat(metadata.started_at)
        self.assertEqual(timedelta(0), started.utcoffset())
        self.assertRegex(metadata.token, r"^[0-9a-f]{32}$")

    def test_second_acquisition_fails_with_validated_owner_metadata(self):
        first = self.new_lease()
        first.acquire()
        self.addCleanup(first.release)
        second = self.new_lease()
        with self.assertRaises(LeaseUnavailable) as caught:
            second.acquire()
        owner = caught.exception.owner
        self.assertIsInstance(owner, LeaseMetadata)
        self.assertEqual(first.metadata.token, owner.token)
        self.assertEqual(os.getpid(), owner.pid)
        self.assertEqual(DEFAULT_ARGV, owner.command_argv)
        self.assertEqual(7, owner.pr)
        self.assertFalse(second.is_held)

    def test_stale_metadata_without_a_held_lock_never_blocks(self):
        self.meta_path().parent.mkdir(parents=True, exist_ok=True)
        self.meta_path().write_text(
            json.dumps({
                "schema_version": 1,
                "pid": 424242,
                "command_argv": ["stale", "owner"],
                "command_display": "stale owner",
                "project": "proj",
                "pr": 7,
                "started_at": "2020-01-01T00:00:00+00:00",
                "token": "f" * 32,
            }),
            encoding="utf-8",
        )
        lease = self.new_lease()
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertTrue(lease.is_held)
        self.assertEqual(os.getpid(), lease.metadata.pid)
        self.assertNotEqual("f" * 32, lease.metadata.token)

    def test_malformed_metadata_without_a_held_lock_never_blocks(self):
        self.meta_path().parent.mkdir(parents=True, exist_ok=True)
        self.meta_path().write_text("not-json {{{", encoding="utf-8")
        lease = self.new_lease()
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertTrue(lease.is_held)

    def test_missing_metadata_never_blocks(self):
        lease = self.new_lease()
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertTrue(lease.is_held)

    def test_metadata_permissions_are_restrictive(self):
        lease = self.new_lease()
        lease.acquire()
        self.addCleanup(lease.release)
        meta_mode = self.meta_path().stat().st_mode & 0o777
        lock_mode = self.lock_path().stat().st_mode & 0o777
        self.assertEqual(0o600, meta_mode)
        self.assertEqual(0o600, lock_mode)

    def test_metadata_is_atomic_json_and_display_round_trips(self):
        argv = ("bash", "-c", "pgrep -f 'x; rm -rf /' | wc -l; echo $(date) `id`")
        lease = HeavyJobLease(self.root, "proj", 11, argv)
        lease.acquire()
        self.addCleanup(lease.release)
        parsed = json.loads(self.meta_path(pr=11).read_text(encoding="utf-8"))
        self.assertIsInstance(parsed["command_argv"], list)
        self.assertEqual(list(argv), parsed["command_argv"])
        self.assertEqual(shlex.join(argv), parsed["command_display"])
        self.assertEqual(list(argv), shlex.split(parsed["command_display"]))
        self.assertEqual(shlex.join(argv), lease.metadata.command_display)
        self.assertEqual(list(argv), shlex.split(lease.metadata.command_display))

    def test_acquire_overwrites_stale_metadata(self):
        self.meta_path().parent.mkdir(parents=True, exist_ok=True)
        self.meta_path().write_text(
            json.dumps({
                "schema_version": 1,
                "pid": 1,
                "command_argv": ["ghost"],
                "command_display": "ghost",
                "project": "proj",
                "pr": 7,
                "started_at": "2020-01-01T00:00:00+00:00",
                "token": "0" * 32,
            }),
            encoding="utf-8",
        )
        lease = self.new_lease()
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertNotEqual("0" * 32, lease.metadata.token)

    def test_reacquire_after_release_on_same_instance(self):
        lease = self.new_lease()
        lease.acquire()
        first_token = lease.metadata.token
        lease.release()
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertTrue(lease.is_held)
        self.assertNotEqual(first_token, lease.metadata.token)


class ReleaseTests(LeaseTestCase):
    def test_release_removes_metadata_and_releases_lock(self):
        lease = self.new_lease()
        lease.acquire()
        lease.release()
        self.assertFalse(lease.is_held)
        self.assertFalse(self.meta_path().exists())
        replacement = self.new_lease()
        replacement.acquire()
        self.assertTrue(replacement.is_held)
        replacement.release()

    def test_double_release_is_safe(self):
        lease = self.new_lease()
        lease.acquire()
        lease.release()
        lease.release()

    def test_release_without_acquire_is_safe(self):
        lease = self.new_lease()
        lease.release()
        self.assertFalse(lease.is_held)

    def test_non_owner_cannot_remove_owner_metadata(self):
        first = self.new_lease()
        first.acquire()
        self.addCleanup(first.release)
        second = self.new_lease()
        with self.assertRaises(LeaseUnavailable):
            second.acquire()
        second.release()
        self.assertTrue(first.is_held)
        self.assertTrue(self.meta_path().exists())

    def test_context_manager_releases_on_body_exception(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with self.new_lease() as lease:
                self.assertTrue(lease.is_held)
                raise RuntimeError("boom")
        self.assertFalse(self.meta_path().exists())
        replacement = self.new_lease()
        replacement.acquire()
        replacement.release()

    def test_release_refuses_to_remove_foreign_token_metadata(self):
        first = self.new_lease()
        first.acquire()
        foreign = {
            "schema_version": 1,
            "pid": 98765,
            "command_argv": ["intruder"],
            "command_display": "intruder",
            "project": "proj",
            "pr": 7,
            "started_at": "2026-08-08T12:00:00+00:00",
            "token": "f" * 32,
        }
        self.meta_path().write_text(json.dumps(foreign), encoding="utf-8")
        with self.assertRaises(LeaseOwnershipError):
            first.release()
        self.assertFalse(first.is_held)
        self.assertEqual("f" * 32, json.loads(self.meta_path().read_text())["token"])
        replacement = self.new_lease()
        replacement.acquire()
        self.addCleanup(replacement.release)
        self.assertNotEqual("f" * 32, replacement.metadata.token)

    def test_release_refuses_to_remove_malformed_metadata(self):
        first = self.new_lease()
        first.acquire()
        self.meta_path().write_text("junk-not-json", encoding="utf-8")
        with self.assertRaises(LeaseMetadataError):
            first.release()
        self.assertFalse(first.is_held)
        self.assertEqual("junk-not-json", self.meta_path().read_text(encoding="utf-8"))
        replacement = self.new_lease()
        replacement.acquire()
        replacement.release()

    def test_descriptor_leak_does_not_survive_normal_release(self):
        first = self.new_lease()
        first.acquire()
        first.release()
        second = self.new_lease()
        second.acquire()
        second.release()
        third = self.new_lease()
        third.acquire()
        self.assertTrue(third.is_held)
        third.release()


class LeaseDescriptorSafetyTests(LeaseTestCase):
    """RED tests: symlink and descriptor-binding path-safety escapes."""

    def valid_foreign_record(self):
        return {
            "schema_version": 1,
            "pid": 98765,
            "command_argv": ["victim", "record"],
            "command_display": "victim record",
            "project": "proj",
            "pr": 7,
            "started_at": "2026-08-08T12:00:00+00:00",
            "token": "f" * 32,
        }

    def test_project_directory_symlink_to_tmp_is_rejected(self):
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        os.symlink(target, self.root / "proj")
        lease = self.new_lease()
        with self.assertRaises(LeasePathEscape):
            lease.acquire()
        self.assertFalse(lease.is_held)
        self.assertEqual([], sorted(p.name for p in target.iterdir()))
        self.assertEqual([], sorted(p.name for p in (self.root / "proj").iterdir()))

    def test_pr_directory_symlink_to_tmp_is_rejected(self):
        (self.root / "proj").mkdir()
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        os.symlink(target, self.root / "proj" / "7")
        with self.assertRaises(LeasePathEscape):
            self.new_lease().acquire()
        self.assertEqual([], sorted(p.name for p in target.iterdir()))

    def test_lock_symlink_to_unrelated_file_is_rejected_untouched(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        victim = self.root / "unrelated.txt"
        victim.write_text("precious bytes", encoding="utf-8")
        os.chmod(victim, 0o644)
        os.symlink(victim, base / "heavy-job.lock")
        with self.assertRaises(LeasePathEscape):
            self.new_lease().acquire()
        self.assertEqual("precious bytes", victim.read_text(encoding="utf-8"))
        self.assertEqual(0o644, victim.stat().st_mode & 0o777)

    def test_dangling_lock_symlink_is_rejected(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        target = self.root / "elsewhere"
        target.mkdir(parents=True)
        os.symlink(target / "ghost.lock", base / "heavy-job.lock")
        with self.assertRaises(LeasePathEscape):
            self.new_lease().acquire()
        self.assertEqual([], sorted(p.name for p in target.iterdir()))

    def test_lock_file_directory_is_rejected(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        os.mkdir(base / "heavy-job.lock")
        with self.assertRaises(LeasePathEscape):
            self.new_lease().acquire()

    def test_lock_file_fifo_is_rejected_without_blocking(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        os.mkfifo(base / "heavy-job.lock")
        with self.assertRaises(LeasePathEscape):
            self.new_lease().acquire()

    def test_multiply_linked_lock_file_is_rejected_untouched(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        lock = base / "heavy-job.lock"
        lock.write_text("shared inode", encoding="utf-8")
        os.chmod(lock, 0o644)
        os.link(lock, self.root / "lock-alias")
        with self.assertRaises(LeasePathEscape):
            self.new_lease().acquire()
        self.assertEqual(2, lock.stat().st_nlink)
        self.assertEqual("shared inode", lock.read_text(encoding="utf-8"))
        self.assertEqual(0o644, lock.stat().st_mode & 0o777)

    def test_metadata_symlink_is_never_followed_for_ownership(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        victim = self.root / "victim.json"
        victim.write_text(json.dumps(self.valid_foreign_record()), encoding="utf-8")
        os.chmod(victim, 0o644)
        first = self.new_lease()
        first.acquire()
        self.addCleanup(first.release)
        os.unlink(base / "heavy-job.meta.json")
        os.symlink(victim, base / "heavy-job.meta.json")
        second = self.new_lease()
        with self.assertRaises(LeaseUnavailable) as caught:
            second.acquire()
        self.assertIsNone(caught.exception.owner)
        self.assertEqual(
            json.dumps(self.valid_foreign_record()), victim.read_text(encoding="utf-8")
        )
        self.assertEqual(0o644, victim.stat().st_mode & 0o777)
        with self.assertRaises(LeaseMetadataError):
            first.release()
        self.assertFalse(first.is_held)
        self.assertTrue((base / "heavy-job.meta.json").is_symlink())
        self.assertEqual(
            json.dumps(self.valid_foreign_record()), victim.read_text(encoding="utf-8")
        )

    def test_metadata_symlink_is_replaced_atomically_without_following(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        victim = self.root / "victim.json"
        victim.write_text("victim-data", encoding="utf-8")
        os.symlink(victim, base / "heavy-job.meta.json")
        lease = self.new_lease()
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertEqual("victim-data", victim.read_text(encoding="utf-8"))
        self.assertFalse((base / "heavy-job.meta.json").is_symlink())
        self.assertTrue((base / "heavy-job.meta.json").is_file())
        self.assertEqual(
            lease.metadata.token,
            json.loads(self.meta_path().read_text(encoding="utf-8"))["token"],
        )

    def test_acquisition_failures_close_every_descriptor(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        os.mkfifo(base / "heavy-job.lock")
        baseline = self.fd_count()
        for _ in range(5):
            with self.assertRaises(LeasePathEscape):
                self.new_lease().acquire()
            self.assertEqual(baseline, self.fd_count())

    def test_held_lock_acquisition_failure_closes_every_descriptor(self):
        first = self.new_lease()
        first.acquire()
        self.addCleanup(first.release)
        baseline = self.fd_count()
        for _ in range(5):
            with self.assertRaises(LeaseUnavailable):
                self.new_lease().acquire()
            self.assertEqual(baseline, self.fd_count())

    def test_repeated_acquire_release_leaks_no_descriptors(self):
        baseline = self.fd_count()
        for _ in range(10):
            lease = self.new_lease()
            lease.acquire()
            lease.release()
        self.assertEqual(baseline, self.fd_count())

    def test_directory_swap_after_acquire_stays_bound_to_original(self):
        lease = self.new_lease()
        lease.acquire()
        base = self.root / "proj" / "7"
        moved = self.root / "proj" / "7-moved"
        os.rename(base, moved)
        os.mkdir(base)
        lease.release()
        self.assertFalse(lease.is_held)
        self.assertEqual([], sorted(p.name for p in base.iterdir()))
        self.assertEqual(["heavy-job.lock"], sorted(p.name for p in moved.iterdir()))

    def test_root_swap_between_construction_and_acquire_fails_closed(self):
        lease = self.new_lease()
        moved = self.root.parent / (self.root.name + "-swapped")
        os.rename(self.root, moved)
        os.mkdir(self.root)
        try:
            with self.assertRaises(LeasePathEscape):
                lease.acquire()
            self.assertFalse(lease.is_held)
            self.assertEqual([], sorted(p.name for p in self.root.iterdir()))
        finally:
            shutil.rmtree(self.root, ignore_errors=True)
            os.rename(moved, self.root)


class LeaseMetadataSafetyTests(LeaseTestCase):
    """RED tests: metadata read must never block and must reject unsafe
    file types with a typed error, without modifying any target."""

    def test_metadata_fifo_read_returns_quickly(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        os.mkfifo(base / "heavy-job.meta.json")
        script = f"""\
from pr_closure.lease import HeavyJobLease, LeaseMetadataError
lease = HeavyJobLease({str(self.root)!r}, "proj", 7, ("bash", "-c", "echo heavy-job"))
try:
    lease.metadata
    print("READ_SUCCEEDED")
except LeaseMetadataError:
    print("READ_REJECTED")
"""
        proc = self.run_bounded_probe(script)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("READ_REJECTED", proc.stdout)

    def test_metadata_fifo_contender_acquire_returns_quickly(self):
        first = self.new_lease()
        first.acquire()
        self.addCleanup(first.release)
        self.meta_path().unlink()
        os.mkfifo(self.meta_path())
        script = f"""\
from pr_closure.lease import HeavyJobLease, LeaseUnavailable
try:
    HeavyJobLease({str(self.root)!r}, "proj", 7, ("bash", "-c", "echo heavy-job")).acquire()
    print("ACQUIRED")
except LeaseUnavailable as error:
    print("UNAVAILABLE owner=" + repr(error.owner))
"""
        proc = self.run_bounded_probe(script)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("UNAVAILABLE owner=None", proc.stdout)

    def test_metadata_fifo_release_returns_quickly(self):
        script = f"""\
import os
from pr_closure.lease import HeavyJobLease, LeaseMetadataError
root = {str(self.root)!r}
lease = HeavyJobLease(root, "proj", 7, ("bash", "-c", "echo heavy-job"))
lease.acquire()
meta = os.path.join(root, "proj", "7", "heavy-job.meta.json")
os.unlink(meta)
os.mkfifo(meta)
try:
    lease.release()
    print("RELEASE_NO_ERROR")
except LeaseMetadataError:
    print("RELEASE_REJECTED")
print("HELD=" + str(lease.is_held))
print("FIFO_EXISTS=" + str(os.path.exists(meta)))
"""
        proc = self.run_bounded_probe(script)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("RELEASE_REJECTED", proc.stdout)
        self.assertIn("HELD=False", proc.stdout)
        self.assertIn("FIFO_EXISTS=True", proc.stdout)

    def test_metadata_directory_is_rejected(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        os.mkdir(base / "heavy-job.meta.json")
        with self.assertRaises(LeaseMetadataError):
            self.new_lease().metadata

    def test_metadata_multiply_linked_is_rejected_untouched(self):
        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        meta = base / "heavy-job.meta.json"
        record = {
            "schema_version": 1,
            "pid": 424242,
            "command_argv": ["heavy", "job"],
            "command_display": "heavy job",
            "project": "proj",
            "pr": 7,
            "started_at": "2026-08-08T12:00:00+00:00",
            "token": "f" * 32,
        }
        meta.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(meta, 0o644)
        os.link(meta, self.root / "meta-alias")
        with self.assertRaises(LeaseMetadataError):
            self.new_lease().metadata
        self.assertEqual(2, meta.stat().st_nlink)
        self.assertEqual(0o644, meta.stat().st_mode & 0o777)
        self.assertEqual(json.dumps(record), meta.read_text(encoding="utf-8"))

    def test_metadata_socket_is_rejected(self):
        import socket

        base = self.root / "proj" / "7"
        base.mkdir(parents=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(base / "heavy-job.meta.json"))
        with self.assertRaises(LeaseMetadataError):
            self.new_lease().metadata


class LeaseMetadataContractTests(LeaseTestCase):
    """RED tests: the JSON projection must carry the exact derived display."""

    def valid_record(self, **overrides):
        record = {
            "schema_version": 1,
            "pid": 424242,
            "command_argv": ["heavy", "job"],
            "command_display": "heavy job",
            "project": "proj",
            "pr": 7,
            "started_at": "2026-08-08T12:00:00+00:00",
            "token": "f" * 32,
        }
        record.update(overrides)
        return record

    def test_missing_command_display_fails_closed(self):
        record = self.valid_record()
        del record["command_display"]
        with self.assertRaises(LeaseMetadataError):
            validate_lease_metadata(record)

    def test_contradictory_command_display_fails_closed(self):
        record = self.valid_record(command_display="heavy job; rm -rf /")
        with self.assertRaises(LeaseMetadataError):
            validate_lease_metadata(record)

    def test_non_string_command_display_fails_closed(self):
        for bad in (None, 7, ["heavy", "job"]):
            with self.subTest(value=bad):
                with self.assertRaises(LeaseMetadataError):
                    validate_lease_metadata(self.valid_record(command_display=bad))


@unittest.skipUnless(hasattr(os, "fork"), "fork required")
class ForkedChildTests(LeaseTestCase):
    def test_inherited_descriptor_cannot_claim_independent_ownership(self):
        lease = self.new_lease()
        lease.acquire()
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            results = []
            try:
                lease.acquire()
                results.append("inherited-acquire-no-error")
            except LeaseOwnershipError:
                results.append("inherited-acquire-ownership-error")
            try:
                lease.release()
                results.append("inherited-release-no-error")
            except LeaseOwnershipError:
                results.append("inherited-release-ownership-error")
            fresh = self.new_lease()
            try:
                fresh.acquire()
                results.append("fresh-acquired")
            except LeaseUnavailable:
                results.append("fresh-unavailable")
            os.write(write_fd, "|".join(results).encode("utf-8"))
            os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        _, status = os.waitpid(child_pid, 0)
        chunks = []
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(read_fd)
        self.assertEqual(0, os.waitstatus_to_exitcode(status))
        self.assertEqual(
            ["inherited-acquire-ownership-error", "inherited-release-ownership-error", "fresh-unavailable"],
            b"".join(chunks).decode("utf-8").split("|"),
        )
        self.assertTrue(lease.is_held)
        self.assertTrue(self.meta_path().exists())
        lease.release()
        self.assertFalse(lease.is_held)
        self.assertFalse(self.meta_path().exists())


class LaneSnapshotTests(LeaseTestCase):
    def valid(self, **overrides):
        data = {
            "lane_id": "ci-full",
            "output_bytes": 0,
            "cpu_seconds": 0.0,
        }
        data.update(overrides)
        return data

    def test_valid_snapshot_accepts_diagnostic_pid_and_recorded_at(self):
        snapshot = validate_lane_snapshot(self.valid(
            pid=1234,
            recorded_at=datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
        ))
        self.assertIsInstance(snapshot, LaneSnapshot)
        self.assertEqual(1234, snapshot.pid)

    def test_unknown_fields_fail_closed(self):
        with self.assertRaisesRegex(LaneSnapshotError, "unknown"):
            validate_lane_snapshot(self.valid(pgrep_matched=True))

    def test_missing_fields_fail_closed(self):
        for missing in ("lane_id", "output_bytes", "cpu_seconds"):
            with self.subTest(missing=missing):
                data = self.valid()
                del data[missing]
                with self.assertRaisesRegex(LaneSnapshotError, missing):
                    validate_lane_snapshot(data)

    def test_shell_text_is_never_a_typed_counter(self):
        for bad in ("0\n0", "0 0", "0", "3.5"):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(LaneSnapshotError, "output_bytes"):
                    validate_lane_snapshot(self.valid(output_bytes=bad))
        with self.assertRaisesRegex(LaneSnapshotError, "cpu_seconds"):
            validate_lane_snapshot(self.valid(cpu_seconds="0 0"))

    def test_float_output_bytes_fails_closed(self):
        with self.assertRaisesRegex(LaneSnapshotError, "output_bytes"):
            validate_lane_snapshot(self.valid(output_bytes=0.0))

    def test_bool_is_not_a_counter(self):
        with self.assertRaisesRegex(LaneSnapshotError, "output_bytes"):
            validate_lane_snapshot(self.valid(output_bytes=True))
        with self.assertRaisesRegex(LaneSnapshotError, "cpu_seconds"):
            validate_lane_snapshot(self.valid(cpu_seconds=True))

    def test_negative_counters_fail_closed(self):
        with self.assertRaisesRegex(LaneSnapshotError, "output_bytes"):
            validate_lane_snapshot(self.valid(output_bytes=-1))
        with self.assertRaisesRegex(LaneSnapshotError, "cpu_seconds"):
            validate_lane_snapshot(self.valid(cpu_seconds=-0.5))

    def test_non_finite_cpu_fails_closed(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(LaneSnapshotError):
                    validate_lane_snapshot(self.valid(cpu_seconds=bad))

    def test_lane_identity_must_be_a_nonempty_string(self):
        for bad in ("", "  ", 7, None):
            with self.subTest(value=bad):
                with self.assertRaises(LaneSnapshotError):
                    validate_lane_snapshot(self.valid(lane_id=bad))

    def test_non_object_snapshot_fails_closed(self):
        for bad in ("0\n0", ["0", "0"], None):
            with self.subTest(value=bad):
                with self.assertRaises(LaneSnapshotError):
                    validate_lane_snapshot(bad)


class LivenessTests(LeaseTestCase):
    def snapshot(self, output_bytes=0, cpu_seconds=0.0, lane_id="ci-full", pid=None, recorded_at=None):
        return LaneSnapshot(
            lane_id=lane_id,
            output_bytes=output_bytes,
            cpu_seconds=cpu_seconds,
            pid=pid,
            recorded_at=recorded_at,
        )

    def test_live_when_output_bytes_grow(self):
        self.assertTrue(is_lane_live(self.snapshot(0, 0.0), self.snapshot(1024, 0.0)))

    def test_live_when_cpu_time_advances(self):
        self.assertTrue(is_lane_live(self.snapshot(0, 0.0), self.snapshot(0, 1.5)))

    def test_not_live_on_unchanged_heartbeat(self):
        self.assertFalse(is_lane_live(self.snapshot(5, 2.0), self.snapshot(5, 2.0)))

    def test_process_existence_alone_is_never_liveness(self):
        self.assertFalse(is_lane_live(
            self.snapshot(0, 0.0, pid=None),
            self.snapshot(0, 0.0, pid=os.getpid()),
        ))

    def test_pid_change_alone_is_never_liveness(self):
        self.assertFalse(is_lane_live(
            self.snapshot(0, 0.0, pid=100),
            self.snapshot(0, 0.0, pid=200),
        ))

    def test_wall_clock_age_alone_is_never_liveness(self):
        now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        later = datetime(2026, 8, 8, 13, 0, tzinfo=timezone.utc)
        self.assertFalse(is_lane_live(
            self.snapshot(0, 0.0, recorded_at=now),
            self.snapshot(0, 0.0, recorded_at=later),
        ))

    def test_counter_regression_fails_closed(self):
        with self.assertRaises(LaneRegressionError):
            is_lane_live(self.snapshot(100, 1.0), self.snapshot(50, 1.0))
        with self.assertRaises(LaneRegressionError):
            is_lane_live(self.snapshot(100, 1.0), self.snapshot(100, 0.5))

    def test_identity_change_fails_closed(self):
        with self.assertRaises(LaneIdentityError):
            is_lane_live(self.snapshot(lane_id="ci-full"), self.snapshot(lane_id="lint"))

    def test_raw_shell_text_is_not_consumed_by_liveness(self):
        with self.assertRaises(TypeError):
            is_lane_live("0\n0", "0\n0")
        with self.assertRaises(TypeError):
            is_lane_live(None, self.snapshot())


class PaidFailureTests(LeaseTestCase):
    def test_self_matching_pgrep_pattern_is_not_liveness(self):
        coordinator = (
            "bash",
            "-c",
            "pgrep -f 'playwright test|docker compose up|npx playwright test'",
        )
        lease = HeavyJobLease(self.root, "proj", 10, coordinator)
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertEqual(coordinator, lease.metadata.command_argv)
        with self.assertRaisesRegex(LaneSnapshotError, "unknown"):
            validate_lane_snapshot({
                "lane_id": "ci-full",
                "output_bytes": 0,
                "cpu_seconds": 0.0,
                "pgrep_lines": 2,
            })
        unchanged = LaneSnapshot("ci-full", 0, 0.0)
        self.assertFalse(is_lane_live(unchanged, unchanged))

    def test_no_match_two_zero_lines_are_not_liveness(self):
        previous = validate_lane_snapshot({
            "lane_id": "worker",
            "output_bytes": 0,
            "cpu_seconds": 0.0,
        })
        current = validate_lane_snapshot({
            "lane_id": "worker",
            "output_bytes": 0,
            "cpu_seconds": 0.0,
        })
        self.assertFalse(is_lane_live(previous, current))


class LockReplacementTests(LeaseTestCase):
    """T5G-F1: the lock directory entry must bind back to the flocked inode.

    A deterministic probe replaces ``heavy-job.lock`` between the descriptor
    open and the flock; the first acquisition must refuse so that a second
    lease can never coexist with a successfully returned first lease.
    """

    def test_lock_replaced_after_open_refuses_first_acquisition(self):
        base = self.root / "proj" / "7"
        real_flock = fcntl.flock
        state = {"swapped": False}

        def swapped_flock(fd, op):
            if not state["swapped"] and op == fcntl.LOCK_EX | fcntl.LOCK_NB:
                state["swapped"] = True
                os.rename(base / "heavy-job.lock", base / "old.lock")
                (base / "heavy-job.lock").write_bytes(b"replacement inode")
            real_flock(fd, op)

        self.addCleanup(setattr, fcntl, "flock", real_flock)
        fcntl.flock = swapped_flock
        first = self.new_lease()
        baseline = self.fd_count()
        with self.assertRaises(LeasePathEscape):
            first.acquire()
        self.assertFalse(first.is_held)
        self.assertEqual(baseline, self.fd_count())
        self.assertFalse(self.meta_path().exists())
        second = self.new_lease()
        second.acquire()
        self.addCleanup(second.release)
        self.assertTrue(second.is_held)

    def test_clean_two_open_contention_control(self):
        first = self.new_lease()
        first.acquire()
        self.addCleanup(first.release)
        second = self.new_lease()
        with self.assertRaises(LeaseUnavailable):
            second.acquire()
        self.assertFalse(second.is_held)
        self.assertTrue(first.is_held)


class MissingRootParentSwapTests(LeaseTestCase):
    """T5G-F2: creating a missing root must never follow a swapped parent."""

    def test_missing_root_parent_swap_to_symlink_creates_nothing(self):
        parent = self.root / "parent"
        root = parent / "root"
        parent.mkdir()
        attacker = self.root / "attacker-target"
        attacker.mkdir()
        lease = HeavyJobLease(root, "proj", 7, DEFAULT_ARGV)
        moved = self.root / "parent-moved"
        os.rename(parent, moved)
        os.symlink(attacker, parent)
        with self.assertRaises(LeasePathEscape):
            lease.acquire()
        self.assertFalse(lease.is_held)
        self.assertEqual([], sorted(p.name for p in attacker.iterdir()))
        self.assertEqual([], sorted(p.name for p in moved.iterdir()))

    def test_unchanged_missing_root_creation_control(self):
        root = self.root / "new" / "root"
        lease = HeavyJobLease(root, "proj", 7, DEFAULT_ARGV)
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertTrue(lease.is_held)
        self.assertTrue((root / "proj" / "7" / "heavy-job.lock").is_file())
        self.assertTrue((root / "proj" / "7" / "heavy-job.meta.json").is_file())


class ReleaseMetadataReplacementTests(LeaseTestCase):
    """T5G-F3: release must unlink only the exact validated metadata inode.

    A deterministic probe swaps ``heavy-job.meta.json`` after the validated
    read; release must fail closed and preserve both the original owner bytes
    (under the probe's backup name) and the unrelated replacement.
    """

    def test_release_refuses_replaced_metadata_entry(self):
        base = self.root / "proj" / "7"
        original_read = lease_module._try_read_metadata

        def swapped_read(fd, path):
            result = original_read(fd, path)
            metadata = result[0] if isinstance(result, tuple) else result
            if metadata is not None:
                os.rename(
                    "heavy-job.meta.json",
                    "owner-backup.json",
                    src_dir_fd=fd,
                    dst_dir_fd=fd,
                )
                with os.fdopen(
                    os.open(
                        "heavy-job.meta.json",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o600,
                        dir_fd=fd,
                    ),
                    "wb",
                ) as handle:
                    handle.write(b"unrelated replacement")
            return result

        first = self.new_lease()
        first.acquire()
        owner_token = json.loads(
            self.meta_path().read_text(encoding="utf-8")
        )["token"]
        self.addCleanup(setattr, lease_module, "_try_read_metadata", original_read)
        lease_module._try_read_metadata = swapped_read
        with self.assertRaises(LeaseMetadataError):
            first.release()
        self.assertFalse(first.is_held)
        self.assertEqual(
            b"unrelated replacement", (base / "heavy-job.meta.json").read_bytes()
        )
        backup = json.loads(
            (base / "owner-backup.json").read_text(encoding="utf-8")
        )
        self.assertEqual(owner_token, backup["token"])

    def test_release_removes_only_owner_metadata_control(self):
        base = self.root / "proj" / "7"
        lease = self.new_lease()
        lease.acquire()
        unrelated = base / "unrelated-bytes.txt"
        unrelated.write_bytes(b"keep me")
        lease.release()
        self.assertFalse(lease.is_held)
        self.assertFalse(self.meta_path().exists())
        self.assertEqual(b"keep me", unrelated.read_bytes())
        replacement = self.new_lease()
        replacement.acquire()
        self.addCleanup(replacement.release)


class MetadataWriteFailureTests(LeaseTestCase):
    """T5G-F4: the raw temporary descriptor must close on every write failure.

    Each probe injects one failure into the metadata write path, then verifies
    acquisition unwinds, no descriptor leaks, no lease stays held, and a fresh
    lease can still acquire.
    """

    def setUp(self):
        super().setUp()
        self._saved_os_attrs = {}

    def install_os_patch(self, name, wrapper):
        self._saved_os_attrs[name] = getattr(os, name)
        setattr(os, name, wrapper)

    def restore_os(self):
        for name, saved in self._saved_os_attrs.items():
            setattr(os, name, saved)
        self._saved_os_attrs.clear()

    def assert_unwinds_without_leak(self, install_failure, expected):
        base = self.root / "proj" / "7"
        baseline = self.fd_count()
        install_failure()
        try:
            with self.assertRaises(expected):
                self.new_lease().acquire()
            self.assertEqual(baseline, self.fd_count())
        finally:
            self.restore_os()
        self.assertEqual(
            [],
            sorted(p.name for p in base.iterdir() if p.name.startswith(".meta-")),
        )
        fresh = self.new_lease()
        fresh.acquire()
        self.addCleanup(fresh.release)
        self.assertTrue(fresh.is_held)

    def test_fdopen_failure_closes_raw_descriptor(self):
        real_fdopen = os.fdopen

        def failing_fdopen(fd, mode, *args, **kwargs):
            if mode == "wb":
                raise RuntimeError("injected fdopen failure")
            return real_fdopen(fd, mode, *args, **kwargs)

        self.assert_unwinds_without_leak(
            lambda: self.install_os_patch("fdopen", failing_fdopen),
            RuntimeError,
        )

    def test_write_failure_closes_descriptor(self):
        real_fdopen = os.fdopen

        def broken_write_fdopen(fd, mode, *args, **kwargs):
            handle = real_fdopen(fd, mode, *args, **kwargs)
            if mode == "wb":

                def boom(*a, **k):
                    raise RuntimeError("injected write failure")

                handle.write = boom
            return handle

        self.assert_unwinds_without_leak(
            lambda: self.install_os_patch("fdopen", broken_write_fdopen),
            RuntimeError,
        )

    def test_flush_failure_closes_descriptor(self):
        real_fdopen = os.fdopen

        def broken_flush_fdopen(fd, mode, *args, **kwargs):
            handle = real_fdopen(fd, mode, *args, **kwargs)
            if mode == "wb":

                def boom(*a, **k):
                    raise RuntimeError("injected flush failure")

                handle.flush = boom
            return handle

        self.assert_unwinds_without_leak(
            lambda: self.install_os_patch("fdopen", broken_flush_fdopen),
            RuntimeError,
        )

    def test_fsync_failure_closes_descriptor(self):
        def failing_fsync(fd):
            raise RuntimeError("injected fsync failure")

        self.assert_unwinds_without_leak(
            lambda: self.install_os_patch("fsync", failing_fsync),
            RuntimeError,
        )

    def test_fchmod_failure_closes_descriptor(self):
        def failing_fchmod(fd, mode):
            raise RuntimeError("injected fchmod failure")

        self.assert_unwinds_without_leak(
            lambda: self.install_os_patch("fchmod", failing_fchmod),
            RuntimeError,
        )

    def test_replace_failure_closes_descriptor_and_cleans_temp(self):
        def failing_replace(*args, **kwargs):
            raise OSError("injected replace failure")

        self.assert_unwinds_without_leak(
            lambda: self.install_os_patch("replace", failing_replace),
            OSError,
        )

    def test_cleanup_failure_after_replace_failure_closes_descriptor(self):
        def failing_replace(*args, **kwargs):
            raise OSError("injected replace failure")

        def failing_unlink(name, *args, **kwargs):
            raise OSError("injected cleanup failure")

        base = self.root / "proj" / "7"
        baseline = self.fd_count()
        self.install_os_patch("replace", failing_replace)
        self.install_os_patch("unlink", failing_unlink)
        try:
            with self.assertRaises(OSError):
                self.new_lease().acquire()
            self.assertEqual(baseline, self.fd_count())
        finally:
            self.restore_os()
        leftovers = sorted(
            p.name for p in base.iterdir() if p.name.startswith(".meta-")
        )
        self.assertEqual(1, len(leftovers))
        (base / leftovers[0]).unlink()
        fresh = self.new_lease()
        fresh.acquire()
        self.addCleanup(fresh.release)
        self.assertTrue(fresh.is_held)


class SchemaVersionTypeTests(LeaseTestCase):
    """T5G-F5: schema_version must be an exact non-bool int equal to 1."""

    def valid_record(self, **overrides):
        record = {
            "schema_version": 1,
            "pid": 424242,
            "command_argv": ["heavy", "job"],
            "command_display": "heavy job",
            "project": "proj",
            "pr": 7,
            "started_at": "2026-08-08T12:00:00+00:00",
            "token": "f" * 32,
        }
        record.update(overrides)
        return record

    def test_schema_version_requires_exact_int_one(self):
        for bad in (True, False, 1.0, "1", 2, 0, None, 1.5):
            with self.subTest(schema_version=bad):
                with self.assertRaises(LeaseMetadataError):
                    validate_lease_metadata(self.valid_record(schema_version=bad))

    def test_malformed_schema_contended_read_returns_no_owner(self):
        for bad in (True, 1.0, "1", 2):
            with self.subTest(schema_version=bad):
                lease = self.new_lease()
                lease.acquire()
                try:
                    token = json.loads(
                        self.meta_path().read_text(encoding="utf-8")
                    )["token"]
                    record = self.valid_record(token=token, schema_version=bad)
                    self.meta_path().write_text(json.dumps(record), encoding="utf-8")
                    second = self.new_lease()
                    with self.assertRaises(LeaseUnavailable) as caught:
                        second.acquire()
                    self.assertIsNone(caught.exception.owner)
                    self.assertFalse(second.is_held)
                    self.assertTrue(lease.is_held)
                finally:
                    try:
                        lease.release()
                    except LeaseError:
                        pass

    def test_malformed_schema_release_refuses_and_preserves(self):
        for bad in (True, 1.0, "1", 2):
            with self.subTest(schema_version=bad):
                lease = self.new_lease()
                lease.acquire()
                token = json.loads(
                    self.meta_path().read_text(encoding="utf-8")
                )["token"]
                record = self.valid_record(token=token, schema_version=bad)
                self.meta_path().write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(LeaseMetadataError):
                    lease.release()
                self.assertFalse(lease.is_held)
                self.assertEqual(
                    json.dumps(record), self.meta_path().read_text(encoding="utf-8")
                )


if __name__ == "__main__":
    unittest.main()
