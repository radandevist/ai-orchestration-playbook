from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import secrets
import shlex
import stat
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional, Tuple

from pr_closure.store import _FORBIDDEN_ROOT_SPECS, _is_forbidden, _resolve

LEASE_SCHEMA_VERSION = 1

LOCK_FILENAME = "heavy-job.lock"
META_FILENAME = "heavy-job.meta.json"

_KNOWN_SNAPSHOT_FIELDS = frozenset(
    ("lane_id", "output_bytes", "cpu_seconds", "pid", "recorded_at")
)


class LeaseError(ValueError):
    """Base class for heavy-job lease failures."""


class LeasePathEscape(LeaseError):
    """Raised when a lease path component would escape the durable root."""


class ForbiddenLeaseRoot(LeaseError):
    """Raised when the lease root lives in a temporary session area."""


class LeaseMetadataError(LeaseError):
    """Raised when lease metadata is missing, malformed, or invalid."""


class LeaseUnavailable(LeaseError):
    """Raised when another process holds the heavy-job lease.

    The lock is the sole ownership authority; ``owner`` is the validated
    diagnostic projection read next to it, or None when the projection is
    unreadable. A held lock never becomes available because of metadata.
    """

    def __init__(self, message: str, owner: Optional["LeaseMetadata"] = None):
        super().__init__(message)
        self.owner = owner


class LeaseOwnershipError(LeaseError):
    """Raised when a non-owner attempts an ownership-only lease operation."""


class LaneHealthError(LeaseError):
    """Base class for lane-health failures."""


class LaneSnapshotError(LaneHealthError):
    """Raised when a lane snapshot is missing fields, has unknown fields,
    or carries malformed or negative counters."""


class LaneIdentityError(LaneHealthError):
    """Raised when a liveness comparison spans two different lanes."""


class LaneRegressionError(LaneHealthError):
    """Raised when a lane's monotonic counters regress between snapshots."""


def _require_component(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LeasePathEscape(f"{label} must be a non-empty component")
    if value in (".", "..") or any(c in value for c in ("/", "\\", "\x00")):
        raise LeasePathEscape(f"{label} must not escape the lease root")
    if value != value.strip():
        raise LeasePathEscape(f"{label} must not have surrounding whitespace")
    return value


def _require_pr(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise LeasePathEscape("pr must be a positive integer")
    return value


def _require_metadata_pid(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise LeasePathEscape("pid must be a positive integer")
    return value


def _require_argv(value) -> Tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise LeaseMetadataError("command_argv must be a non-empty list of strings")
    argv = tuple(value)
    if not all(isinstance(part, str) and part for part in argv):
        raise LeaseMetadataError("command_argv must be a list of non-empty strings")
    return argv


def _require_command_display(value, argv: Tuple[str, ...]) -> str:
    if not isinstance(value, str):
        raise LeaseMetadataError("command_display must be a string")
    if value != shlex.join(argv):
        raise LeaseMetadataError(
            "command_display must exactly equal shlex.join(command_argv)"
        )
    return value


def _require_token(value) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 16
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise LeaseMetadataError(
            "token must be a hex ownership token of at least 16 characters"
        )
    return value


def _require_utc_iso(value) -> str:
    if not isinstance(value, str):
        raise LeaseMetadataError("started_at must be an ISO 8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise LeaseMetadataError(
            f"started_at must be an ISO 8601 UTC timestamp: {error}"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise LeaseMetadataError("started_at must carry a UTC offset")
    return value


def _require_lane_id(value) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise LaneSnapshotError("lane_id must be a non-empty string without surrounding whitespace")
    return value


def _require_output_bytes(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LaneSnapshotError("output_bytes must be a non-negative integer")
    return value


def _require_cpu_seconds(value) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise LaneSnapshotError("cpu_seconds must be a non-negative number")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise LaneSnapshotError("cpu_seconds must be a finite non-negative number")
    return converted


def _require_pid(value) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise LaneSnapshotError("pid must be a positive integer")
    return value


def _require_recorded_at(value) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise LaneSnapshotError("recorded_at must be a datetime")
    return value


@dataclass(frozen=True)
class LeaseMetadata:
    """Diagnostic projection next to the lock. Never decides ownership.

    ``command_argv`` is the canonical typed value; ``command_display`` is
    its diagnostic :func:`shlex.join` projection, which safely round-trips
    with :func:`shlex.split` and is never executed by this module.
    """

    schema_version: int
    pid: int
    command_argv: Tuple[str, ...]
    command_display: str
    project: str
    pr: int
    started_at: str
    token: str

    def __post_init__(self):
        if self.schema_version != LEASE_SCHEMA_VERSION:
            raise LeaseMetadataError(
                f"unsupported lease schema version: {self.schema_version}"
            )
        try:
            object.__setattr__(self, "pid", _require_metadata_pid(self.pid))
            object.__setattr__(self, "project", _require_component(self.project, "project"))
            object.__setattr__(self, "pr", _require_pr(self.pr))
        except LeasePathEscape as error:
            raise LeaseMetadataError(f"invalid lease metadata: {error}") from error
        object.__setattr__(self, "command_argv", _require_argv(self.command_argv))
        object.__setattr__(
            self,
            "command_display",
            _require_command_display(self.command_display, self.command_argv),
        )
        object.__setattr__(self, "started_at", _require_utc_iso(self.started_at))
        object.__setattr__(self, "token", _require_token(self.token))

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "pid": self.pid,
            "command_argv": list(self.command_argv),
            "command_display": self.command_display,
            "project": self.project,
            "pr": self.pr,
            "started_at": self.started_at,
            "token": self.token,
        }


def validate_lease_metadata(data: Mapping) -> LeaseMetadata:
    """Validate raw metadata (JSON object) into a :class:`LeaseMetadata`."""
    if not isinstance(data, dict):
        raise LeaseMetadataError("lease metadata must be a JSON object")
    known = {field.name for field in fields(LeaseMetadata)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise LeaseMetadataError(f"unknown lease metadata fields: {', '.join(unknown)}")
    missing = sorted(known - set(data))
    if missing:
        raise LeaseMetadataError(f"missing lease metadata fields: {', '.join(missing)}")
    return LeaseMetadata(**data)


def _try_read_metadata(fd: int, path: Path) -> Optional[LeaseMetadata]:
    """Read and validate the metadata projection next to the open lock.

    The read is bound to the validated PR directory descriptor ``fd`` and
    never follows symlinks: a symbolic ``heavy-job.meta.json`` entry raises
    :class:`LeaseMetadataError` and its target is never opened or modified.
    The entry is opened with ``O_NONBLOCK`` and the opened inode is
    ``fstat``-checked before any byte is read: a FIFO, directory, socket,
    device, or multiply-linked regular file raises :class:`LeaseMetadataError`
    immediately instead of blocking the reader. Returns None when the file
    does not exist. Raises :class:`LeaseMetadataError` when the file exists
    but cannot be validated (fail closed). The descriptor is closed on every
    path.
    """
    meta_fd = None
    try:
        try:
            meta_fd = os.open(
                META_FILENAME,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise LeaseMetadataError(
                f"cannot read lease metadata {path}: {error}"
            ) from error
        try:
            st = os.fstat(meta_fd)
        except OSError as error:
            raise LeaseMetadataError(
                f"cannot inspect lease metadata {path}: {error}"
            ) from error
        if not stat.S_ISREG(st.st_mode):
            raise LeaseMetadataError(
                f"lease metadata {path} is not a regular file; refusing to read it"
            )
        if st.st_nlink != 1:
            raise LeaseMetadataError(
                f"lease metadata {path} is multiply linked; refusing to read it"
            )
        try:
            with os.fdopen(meta_fd, "r", encoding="utf-8") as handle:
                meta_fd = None
                raw = handle.read()
        except UnicodeDecodeError as error:
            raise LeaseMetadataError(f"malformed lease metadata {path}: {error}") from error
        except OSError as error:
            raise LeaseMetadataError(
                f"cannot read lease metadata {path}: {error}"
            ) from error
    finally:
        if meta_fd is not None:
            try:
                os.close(meta_fd)
            except OSError:
                pass
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LeaseMetadataError(f"malformed lease metadata {path}: {error}") from error
    return validate_lease_metadata(parsed)


@dataclass(frozen=True)
class LaneSnapshot:
    """Typed lane-health counters. ``pid`` and ``recorded_at`` are diagnostic
    only: process existence, wall-clock age, and heartbeats are never liveness."""

    lane_id: str
    output_bytes: int
    cpu_seconds: float
    pid: Optional[int] = None
    recorded_at: Optional[datetime] = None

    def __post_init__(self):
        object.__setattr__(self, "lane_id", _require_lane_id(self.lane_id))
        object.__setattr__(self, "output_bytes", _require_output_bytes(self.output_bytes))
        object.__setattr__(self, "cpu_seconds", _require_cpu_seconds(self.cpu_seconds))
        object.__setattr__(self, "pid", _require_pid(self.pid))
        object.__setattr__(self, "recorded_at", _require_recorded_at(self.recorded_at))


def validate_lane_snapshot(data: Mapping) -> LaneSnapshot:
    """Validate raw snapshot data (typed values, never shell text)."""
    if not isinstance(data, dict):
        raise LaneSnapshotError("lane snapshot must be a JSON object")
    unknown = sorted(set(data) - _KNOWN_SNAPSHOT_FIELDS)
    if unknown:
        raise LaneSnapshotError(f"unknown lane snapshot fields: {', '.join(unknown)}")
    required = ("lane_id", "output_bytes", "cpu_seconds")
    missing = [name for name in required if name not in data]
    if missing:
        raise LaneSnapshotError(f"missing lane snapshot fields: {', '.join(missing)}")
    return LaneSnapshot(**data)


def is_lane_live(previous: LaneSnapshot, current: LaneSnapshot) -> bool:
    """Pure liveness decision over typed snapshots.

    Live only when output bytes grow or cumulative CPU time advances. PID
    presence or change, unchanged heartbeats, wall-clock age, and command-line
    pattern matches are never liveness. Identity change and counter regression
    fail closed with typed errors.
    """
    if not isinstance(previous, LaneSnapshot) or not isinstance(current, LaneSnapshot):
        raise TypeError("is_lane_live requires typed LaneSnapshot values")
    if previous.lane_id != current.lane_id:
        raise LaneIdentityError(
            f"lane identity changed from {previous.lane_id!r} to {current.lane_id!r}"
        )
    if current.output_bytes < previous.output_bytes or current.cpu_seconds < previous.cpu_seconds:
        raise LaneRegressionError(
            "lane counters regressed: "
            f"bytes {previous.output_bytes} -> {current.output_bytes}, "
            f"cpu {previous.cpu_seconds} -> {current.cpu_seconds}"
        )
    return current.output_bytes > previous.output_bytes or current.cpu_seconds > previous.cpu_seconds


class HeavyJobLease:
    """Exclusive heavy-job lease.

    Ownership authority is the exclusive non-blocking ``flock`` held on the
    lock descriptor. ``heavy-job.meta.json`` is a diagnostic projection: stale,
    malformed, or missing metadata never blocks acquisition, never grants
    ownership, and is removed on release only when the held descriptor and
    ownership token belong to this lease.

    Layout under ``root/project/pr``:
      heavy-job.lock         the flock target (authority)
      heavy-job.meta.json    diagnostic projection (never authority)

    Path safety: every filesystem operation is bound to opened directory
    descriptors. The root, project, and PR directories are opened one
    component at a time with ``dir_fd`` and ``O_DIRECTORY | O_NOFOLLOW``, and
    each opened directory's identity is verified against the expected durable
    root before any lock or metadata operation. ``lock_path``/``meta_path``
    are diagnostic paths only; their unresolved string form is never used as
    ownership authority.
    """

    _OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def __init__(self, root, project, pr, command_argv):
        root_path = os.fspath(root)
        if not os.path.isabs(root_path):
            raise ForbiddenLeaseRoot("lease root must be an absolute path")
        resolved_root = _resolve(root_path)
        if _is_forbidden(resolved_root):
            raise ForbiddenLeaseRoot(
                "lease root must not live under a temporary session area"
            )
        self._root = Path(resolved_root)
        self._project = _require_component(project, "project")
        self._pr = _require_pr(pr)
        self._command_argv = _require_argv(command_argv)
        self._base = self._root / self._project / str(self._pr)
        self._lock_path = self._base / LOCK_FILENAME
        self._meta_path = self._base / META_FILENAME
        self._root_identity = None
        try:
            self._root_identity = os.stat(resolved_root)
        except OSError:
            self._root_identity = None
        self._fd: Optional[int] = None
        self._root_fd: Optional[int] = None
        self._pr_fd: Optional[int] = None
        self._held = False
        self._acquired_pid: Optional[int] = None
        self._token: Optional[str] = None

    @property
    def project(self) -> str:
        return self._project

    @property
    def pr(self) -> int:
        return self._pr

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def meta_path(self) -> Path:
        return self._meta_path

    @property
    def is_held(self) -> bool:
        return self._held

    @property
    def metadata(self) -> Optional[LeaseMetadata]:
        """Validated diagnostic projection on disk, or None when absent.

        Reads are bound to the held PR directory descriptor when the lease is
        held; otherwise a transient validated open of the PR directory is used.
        Symlink metadata entries are never followed.
        """
        if self._held:
            return _try_read_metadata(self._pr_fd, self._meta_path)
        root_fd, pr_fd = self._open_dir_chain(create=False)
        if pr_fd is None:
            return None
        try:
            return _try_read_metadata(pr_fd, self._meta_path)
        finally:
            os.close(pr_fd)
            os.close(root_fd)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False

    def __del__(self):
        if getattr(self, "_held", False):
            try:
                self.release()
            except Exception:
                pass

    def _require_same_process(self) -> None:
        if self._acquired_pid is not None and os.getpid() != self._acquired_pid:
            raise LeaseOwnershipError(
                f"lease acquired by pid {self._acquired_pid} cannot be used by "
                f"pid {os.getpid()}; inherited descriptors never claim ownership"
            )

    def _open_root(self, create: bool = True) -> Optional[int]:
        root = os.fspath(self._root)
        try:
            try:
                fd = os.open(root, self._OPEN_DIR_FLAGS)
            except FileNotFoundError:
                if not create:
                    return None
                os.makedirs(root, mode=0o700, exist_ok=True)
                fd = os.open(root, self._OPEN_DIR_FLAGS)
        except OSError as error:
            raise LeasePathEscape(
                f"cannot open lease root {self._root}: {error.strerror or error}"
            ) from error
        try:
            self._assert_root_identity(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _assert_root_identity(self, fd: int) -> None:
        if self._root_identity is None:
            return
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (
            self._root_identity.st_dev,
            self._root_identity.st_ino,
        ):
            raise LeasePathEscape(
                f"lease root changed since construction; refusing to operate "
                f"on a replacement: {self._root}"
            )

    def _open_dir_component(self, parent_fd: int, component, expected: Path, create: bool) -> Optional[int]:
        try:
            try:
                fd = os.open(component, self._OPEN_DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                fd = os.open(component, self._OPEN_DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise LeasePathEscape(
                f"cannot open lease directory {expected}: {error.strerror or error}"
            ) from error
        try:
            self._assert_dir_identity(fd, expected)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _assert_dir_identity(self, fd: int, expected: Path) -> None:
        try:
            named = os.stat(expected)
        except OSError as error:
            raise LeasePathEscape(
                f"cannot verify lease directory {expected}: {error.strerror or error}"
            ) from error
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise LeasePathEscape(
                f"lease directory changed during acquisition; refusing the "
                f"replacement target: {expected}"
            )

    def _open_dir_chain(self, create: bool = True) -> Tuple[Optional[int], Optional[int]]:
        """Open root and PR directory descriptors one component at a time.

        Returns ``(root_fd, pr_fd)``. With ``create=False`` a missing
        component returns ``(None, None)`` without creating anything. Every
        opened descriptor is closed on failure; the project descriptor is
        closed before returning.
        """
        root_fd = self._open_root(create=create)
        if root_fd is None:
            return None, None
        proj_fd = None
        pr_fd = None
        success = False
        try:
            proj_fd = self._open_dir_component(
                root_fd, self._project, self._root / self._project, create=create
            )
            pr_fd = self._open_dir_component(proj_fd, str(self._pr), self._base, create=create)
            success = True
        except FileNotFoundError:
            if create:
                raise LeasePathEscape(
                    f"lease directories vanished during acquisition: {self._base}"
                )
            return None, None
        finally:
            if not success:
                for fd in (pr_fd, proj_fd, root_fd):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
        os.close(proj_fd)
        return root_fd, pr_fd

    def _assert_regular_single_link(self, fd: int) -> None:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LeasePathEscape(
                f"lock file {self._lock_path} is not a regular file; refusing "
                f"to lock or modify it"
            )
        if st.st_nlink != 1:
            raise LeasePathEscape(
                f"lock file {self._lock_path} is multiply linked; refusing to "
                f"lock or modify its shared inode"
            )

    def _open_lock(self, pr_fd: int) -> int:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        for _ in range(8):
            created = False
            try:
                try:
                    fd = os.open(LOCK_FILENAME, flags, dir_fd=pr_fd)
                except FileNotFoundError:
                    fd = os.open(
                        LOCK_FILENAME,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=pr_fd,
                    )
                    created = True
            except FileExistsError:
                continue
            except OSError as error:
                raise LeasePathEscape(
                    f"cannot open lock file {self._lock_path}: "
                    f"{error.strerror or error}"
                ) from error
            try:
                self._assert_regular_single_link(fd)
                if created:
                    os.fchmod(fd, 0o600)
                return fd
            except BaseException:
                os.close(fd)
                raise
        raise LeasePathEscape(
            f"lock file {self._lock_path} kept changing during acquisition"
        )

    def acquire(self):
        """Acquire the exclusive lease, writing the metadata projection.

        Raises :class:`LeaseUnavailable` when another process holds the lock.
        The lock is the sole authority; the metadata projection on disk is
        overwritten, never consulted, when acquisition succeeds. All directory
        and lock descriptors are closed on every acquisition failure and on
        release.
        """
        self._require_same_process()
        if self._held:
            return self
        root_fd = None
        pr_fd = None
        lock_fd = None
        try:
            root_fd, pr_fd = self._open_dir_chain(create=True)
            if pr_fd is None:
                raise LeasePathEscape(
                    f"cannot bind lease directory descriptors for {self._base}"
                )
            lock_fd = self._open_lock(pr_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                os.close(lock_fd)
                lock_fd = None
                if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    owner = None
                    try:
                        owner = _try_read_metadata(pr_fd, self._meta_path)
                    except LeaseMetadataError:
                        owner = None
                    raise LeaseUnavailable(
                        "heavy-job lease is held by another process for "
                        f"{self._project} pr {self._pr}",
                        owner=owner,
                    ) from error
                raise
            self._root_fd = root_fd
            self._pr_fd = pr_fd
            self._fd = lock_fd
            root_fd = pr_fd = lock_fd = None
            self._held = True
            self._acquired_pid = os.getpid()
            self._token = secrets.token_hex(16)
            try:
                self._write_metadata()
            except BaseException:
                self._close_all()
                raise
            return self
        finally:
            for fd in (lock_fd, pr_fd, root_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _write_metadata(self) -> None:
        metadata = LeaseMetadata(
            schema_version=LEASE_SCHEMA_VERSION,
            pid=os.getpid(),
            command_argv=self._command_argv,
            command_display=shlex.join(self._command_argv),
            project=self._project,
            pr=self._pr,
            started_at=datetime.now(timezone.utc).isoformat(),
            token=self._token,
        )
        line = json.dumps(metadata.to_dict(), indent=2, sort_keys=True).encode("utf-8")
        temp_name = f".meta-{secrets.token_hex(8)}"
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._pr_fd,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o600)
            os.replace(
                temp_name,
                META_FILENAME,
                src_dir_fd=self._pr_fd,
                dst_dir_fd=self._pr_fd,
            )
        finally:
            try:
                os.unlink(temp_name, dir_fd=self._pr_fd)
            except OSError:
                pass

    def release(self) -> None:
        """Release the lease and remove its metadata projection.

        The projection is removed only while this lease holds the lock and its
        ownership token still matches the file. Double release and release
        without acquisition are safe no-ops. A foreign or unvalidatable
        projection is never removed (fail closed); the lock and every
        directory descriptor are always closed so nothing leaks.
        """
        if not self._held:
            return
        self._require_same_process()
        try:
            metadata = _try_read_metadata(self._pr_fd, self._meta_path)
            if metadata is not None and metadata.token != self._token:
                raise LeaseOwnershipError(
                    "lease metadata token does not belong to this lease; "
                    "refusing to remove another owner's projection"
                )
            if metadata is not None:
                try:
                    os.unlink(META_FILENAME, dir_fd=self._pr_fd)
                except FileNotFoundError:
                    pass
        finally:
            self._close_all()

    def _close_all(self) -> None:
        fd, self._fd = self._fd, None
        self._held = False
        self._token = None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(fd)
        for attr in ("_pr_fd", "_root_fd"):
            dir_fd = getattr(self, attr, None)
            setattr(self, attr, None)
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
