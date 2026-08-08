from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Optional, Tuple

from pr_closure.contract import COMMIT_ID_PATTERN


class StoreError(ValueError):
    """Base class for durable evidence-store failures."""


class EvidenceConflict(StoreError):
    """Raised when an existing artifact id would be overwritten with different bytes."""


class ForbiddenEvidencePath(StoreError):
    """Raised when an evidence root or evidence path lives in a temporary session area."""


class StorePathEscape(StoreError):
    """Raised when a project/pr/review-id component would escape the store root."""


class MalformedEvidence(StoreError):
    """Raised when an event or artifact cannot be parsed as authoritative evidence."""


class ApprovalState(StrEnum):
    APPROVED = "APPROVED"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"


COMMIT_EVENT = "commit"
VERIFICATION_EVENT = "verification"
REVIEW_EVENT = "review"

EVENT_SCHEMA_VERSION = 1

_REQUIRED_EVENT_KEYS = (
    "schema_version",
    "timestamp",
    "project",
    "pr",
    "commit",
    "event_type",
    "evidence_path",
)

_RESERVED_EVENT_KEYS = frozenset(_REQUIRED_EVENT_KEYS)

# Event types the store vets as authoritative. Later tasks that add custom event
# types must register them here so both write and read sides accept them.
KNOWN_EVENT_TYPES = frozenset(
    (COMMIT_EVENT, VERIFICATION_EVENT, REVIEW_EVENT, "STAGNATION_CONFIG", "REPAIR_STRATEGY")
)

# Non-durable session areas. Evidence rooted here (or anywhere beneath them,
# including through symlinks) is never authoritative.
_FORBIDDEN_ROOT_SPECS = ("/tmp", os.path.expanduser("~/.claude/jobs"))

_COMMIT_ID_RE = re.compile(COMMIT_ID_PATTERN)
_UNSAFE_COMPONENT_RE = re.compile(r"[/\\\x00]")


def _resolve(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _forbidden_roots() -> Tuple[str, ...]:
    return tuple(_resolve(spec) for spec in _FORBIDDEN_ROOT_SPECS)


def _is_forbidden(resolved: str) -> bool:
    for root in _forbidden_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


def _require_component(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StorePathEscape(f"{label} must be a non-empty component")
    if value in (".", "..") or _UNSAFE_COMPONENT_RE.search(value):
        raise StorePathEscape(f"{label} must not escape the store root")
    if value != value.strip():
        raise StorePathEscape(f"{label} must not have surrounding whitespace")
    return value


def _require_pr(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise StorePathEscape("pr must be a positive integer")
    return value


def _require_commit(commit) -> str:
    if not isinstance(commit, str) or _COMMIT_ID_RE.fullmatch(commit) is None:
        raise MalformedEvidence("commit must be a 40-character lowercase hexadecimal id")
    return commit


def _require_review_id(value: str) -> str:
    return _require_component(value, "review_id")


def _require_durable(path, label: str = "evidence path") -> str:
    if not isinstance(path, str) or not path.strip():
        raise ForbiddenEvidencePath(f"{label} must be a non-empty absolute path")
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        raise ForbiddenEvidencePath(f"{label} must be an absolute path")
    resolved = os.path.realpath(expanded)
    if _is_forbidden(resolved):
        raise ForbiddenEvidencePath(
            f"{label} must not live under a temporary session area: {resolved}"
        )
    return resolved


def _require_json_object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise MalformedEvidence(f"{label} must be a JSON object")
    return value


def _serialize(record: Mapping) -> bytes:
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_json_object(path: Path, label: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise MalformedEvidence(f"cannot read {label}: {path}: {error}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MalformedEvidence(f"malformed {label}: {path}: {error}") from error
    return _require_json_object(parsed, f"{label}: {path}")


class RunStore:
    """Durable append-only evidence store for one project pull request.

    Layout under ``root/project/pr``:
      events.jsonl                    append-only audit trail
      verification/<commit>.json      atomic local-gate record
      reviews/<commit>/<review-id>.json  atomic review records
    """

    def __init__(self, root, project, pr):
        root_path = os.fspath(root)
        if not os.path.isabs(root_path):
            raise ForbiddenEvidencePath("store root must be an absolute path")
        resolved_root = _resolve(root_path)
        if _is_forbidden(resolved_root):
            raise ForbiddenEvidencePath(
                "store root must not live under a temporary session area"
            )
        self._root = Path(resolved_root)
        self._project = _require_component(project, "project")
        self._pr = _require_pr(pr)
        self._base = self._root / self._project / str(self._pr)

    @property
    def project(self) -> str:
        return self._project

    @property
    def pr(self) -> int:
        return self._pr

    @property
    def base_dir(self) -> Path:
        return self._base

    @property
    def events_path(self) -> Path:
        return self._base / "events.jsonl"

    def verification_path(self, commit) -> Path:
        return self._base / "verification" / f"{_require_commit(commit)}.json"

    def review_dir(self, commit) -> Path:
        return self._base / "reviews" / _require_commit(commit)

    def review_path(self, commit, review_id) -> Path:
        return self.review_dir(commit) / f"{_require_review_id(review_id)}.json"

    # -- events ------------------------------------------------------------

    def append_event(self, event_type, commit, evidence_path, payload: Optional[Mapping] = None) -> dict:
        """Append one newline-delimited JSON event without touching prior bytes."""
        if not isinstance(event_type, str) or event_type not in KNOWN_EVENT_TYPES:
            raise MalformedEvidence(
                f"unknown event_type: {event_type!r}; known types: "
                f"{', '.join(sorted(KNOWN_EVENT_TYPES))}"
            )
        commit = _require_commit(commit)
        evidence = _require_durable(evidence_path)
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "project": self._project,
            "pr": self._pr,
            "commit": commit,
            "event_type": event_type,
            "evidence_path": evidence,
        }
        if payload is not None:
            payload = _require_json_object(payload, "payload")
            reserved = sorted(key for key in payload if key in _RESERVED_EVENT_KEYS)
            if reserved:
                raise MalformedEvidence(
                    "payload must not override reserved event key(s): "
                    + ", ".join(reserved)
                )
            event.update(payload)
        line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        self._append_line(line)
        return event

    def record_commit(self, commit, evidence_path, payload: Optional[Mapping] = None) -> dict:
        """Record a newly observed pushed commit as the current tip."""
        return self.append_event(COMMIT_EVENT, commit, evidence_path, payload)

    def _require_event_envelope(self, event: dict, number: int, path: Path) -> None:
        def fail(detail: str) -> None:
            raise MalformedEvidence(f"event at line {number}: {path}: {detail}")

        schema_version = event.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != EVENT_SCHEMA_VERSION
        ):
            fail(f"unsupported schema_version: {schema_version!r}")
        project = event.get("project")
        if not isinstance(project, str) or project != self._project:
            fail(f"project does not bind to configured project: {project!r}")
        pr = event.get("pr")
        if not isinstance(pr, int) or isinstance(pr, bool) or pr != self._pr:
            fail(f"pr does not bind to configured pr: {pr!r}")
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or event_type not in KNOWN_EVENT_TYPES:
            fail(f"unknown event_type: {event_type!r}")
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str):
            fail(f"timestamp must be a string: {timestamp!r}")
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            fail(f"timestamp is not valid ISO-8601: {timestamp!r}")
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            fail(f"timestamp must be timezone-aware UTC: {timestamp!r}")
        try:
            _require_durable(event.get("evidence_path"))
        except StoreError:
            fail(f"non-durable evidence_path: {event.get('evidence_path')!r}")
        _require_commit(event.get("commit"))

    def read_events(self) -> Tuple[dict, ...]:
        """Return every stored event in file order, raising on any malformed line."""
        path = self.events_path
        if not path.exists():
            return ()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            raise MalformedEvidence(f"cannot read events: {path}: {error}") from error
        lines = raw.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        events = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                raise MalformedEvidence(f"blank line in events at line {number}: {path}")
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as error:
                raise MalformedEvidence(
                    f"malformed event at line {number}: {path}: {error}"
                ) from error
            event = _require_json_object(parsed, f"event at line {number}")
            missing = [key for key in _REQUIRED_EVENT_KEYS if key not in event]
            if missing:
                raise MalformedEvidence(
                    f"event at line {number} missing key(s): {', '.join(missing)}"
                )
            self._require_event_envelope(event, number, path)
            events.append(event)
        return tuple(events)

    def current_commit(self) -> Optional[str]:
        """Most recent recorded pushed commit, or None before the first commit event."""
        current = None
        for event in self.read_events():
            if event["event_type"] == COMMIT_EVENT:
                current = event["commit"]
        return current

    def _append_line(self, line: bytes) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        fd = os.open(os.fspath(self.events_path), flags, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- verification records ---------------------------------------------

    def write_verification(self, commit, record: Mapping) -> Path:
        """Atomically no-replace ``verification/<commit>.json`` plus its event.

        Different bytes never replace an existing record: the losing writer
        raises ``EvidenceConflict`` and the original bytes survive. An
        identical-byte retry repairs a missing event rather than early-returning.
        """
        commit = _require_commit(commit)
        target = self.verification_path(commit)
        record = _require_json_object(record, "verification record")
        if "commit" in record and record["commit"] != commit:
            raise MalformedEvidence(
                "verification record commit contradicts its store path"
            )
        line = _serialize(record)
        return self._commit_record(target, line, VERIFICATION_EVENT, commit)

    def verification_exists(self, commit) -> bool:
        return self.verification_path(commit).is_file()

    def read_verification(self, commit) -> dict:
        return _read_json_object(self.verification_path(commit), "verification record")

    # -- review records ----------------------------------------------------

    def write_review(self, commit, review_id, record: Mapping) -> Path:
        """Atomically no-replace ``reviews/<commit>/<review-id>.json`` plus its event.

        An existing review id may be written again only with identical bytes;
        different bytes raise ``EvidenceConflict`` and preserve the original.
        Concurrent different-byte writers of the same id race on an atomic
        no-replace create, so exactly one succeeds and the losers conflict.
        """
        commit = _require_commit(commit)
        review_id = _require_review_id(review_id)
        target = self.review_path(commit, review_id)
        record = _require_json_object(record, "review record")
        if "reviewed_commit" in record and record["reviewed_commit"] != commit:
            raise MalformedEvidence("review record reviewed_commit contradicts its store path")
        line = _serialize(record)
        return self._commit_record(target, line, REVIEW_EVENT, commit)

    def review_exists(self, commit, review_id) -> bool:
        return self.review_path(commit, review_id).is_file()

    def read_review(self, commit, review_id) -> dict:
        return _read_json_object(self.review_path(commit, review_id), "review record")

    def review_paths(self, commit) -> Tuple[Path, ...]:
        directory = self.review_dir(commit)
        if not directory.is_dir():
            return ()
        return tuple(sorted(path for path in directory.iterdir() if path.name.endswith(".json")))

    def read_reviews(self, commit) -> Tuple[dict, ...]:
        return tuple(self.read_review(commit, path.stem) for path in self.review_paths(commit))

    # -- derivation --------------------------------------------------------

    def approval_status(self, commit) -> ApprovalState:
        """Derive durable-approval status for one commit, failing closed.

        APPROVED only when an observed current COMMIT_EVENT equals ``commit``,
        a valid VERIFICATION_EVENT binds the queried commit to the resolved
        verification artifact path, and at least one valid REVIEW_EVENT binds it
        to a resolved review artifact path, with both artifacts present and
        parseable. A file with no matching event is orphaned evidence and never
        approves. A newer commit event downgrades any older approval to STALE.
        A missing current-tip event, a missing artifact, or malformed data
        derives UNVERIFIED or raises rather than passing.
        """
        commit = _require_commit(commit)
        events = self.read_events()

        tip = None
        for event in events:
            if event["event_type"] == COMMIT_EVENT:
                tip = event["commit"]

        verification = self.verification_path(commit)
        verification_ok = False
        if verification.is_file():
            if not self._has_matching_event(events, VERIFICATION_EVENT, commit, verification):
                return ApprovalState.UNVERIFIED
            self.read_verification(commit)
            verification_ok = True

        review_ok = False
        for path in self.review_paths(commit):
            if not self._has_matching_event(events, REVIEW_EVENT, commit, path):
                continue
            self.read_review(commit, path.stem)
            review_ok = True
            break

        if not verification_ok or not review_ok:
            return ApprovalState.UNVERIFIED
        if tip is None:
            return ApprovalState.UNVERIFIED
        if tip != commit:
            return ApprovalState.STALE
        return ApprovalState.APPROVED

    def _has_matching_event(self, events, event_type, commit, target: Path) -> bool:
        """Whether an event of ``event_type`` binds ``commit`` to the resolved
        artifact path ``target`` currently resolves to."""
        resolved = _resolve(os.fspath(target))
        return any(
            event["event_type"] == event_type
            and event["commit"] == commit
            and event["evidence_path"] == resolved
            for event in events
        )

    # -- atomic record commit ---------------------------------------------

    def _commit_record(self, target: Path, line: bytes, event_type, commit) -> Path:
        """Commit a record artifact and its evidence event as one transaction.

        The target is re-resolved against the durable-path rules before any
        mutation, so a swapped or symlinked verification/reviews directory
        cannot create the artifact under a forbidden area and fail only after
        the fact. The artifact is created with an atomic no-replace primitive;
        exactly one different-byte writer wins, and losers compare the committed
        bytes and raise ``EvidenceConflict``. The event is appended only after
        the artifact exists; if the append fails, the artifact created by this
        call is rolled back. An identical-byte retry repairs a missing event
        instead of taking an early return, while an already-complete write stays
        idempotent without a duplicate event.
        """
        _require_durable(os.fspath(target), "record path")
        created = self._atomic_create(target, line)
        if not created:
            try:
                existing = target.read_bytes()
            except OSError as error:
                raise MalformedEvidence(
                    f"cannot read existing record: {target}: {error}"
                ) from error
            if existing != line:
                raise EvidenceConflict(
                    f"record already exists with different bytes: {target}"
                )
        if self._has_matching_event(self.read_events(), event_type, commit, target):
            return target
        try:
            self.append_event(event_type, commit, os.fspath(target))
        except BaseException:
            if created:
                try:
                    os.unlink(os.fspath(target))
                except OSError:
                    pass
            raise
        return target

    def _atomic_create(self, target: Path, line: bytes) -> bool:
        """Create ``target`` with ``line`` using an atomic no-replace primitive.

        Returns True when this call created the target; False when it already
        existed (or a concurrent writer created it first). The temporary file is
        always removed, and the parent directory is fsynced when a new entry is
        made. ``os.link`` fails with ``EEXIST`` if the target already exists, so
        two writers cannot both create the same path.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=os.fspath(target.parent), mode="wb", prefix=".tmp-", delete=False
            ) as handle:
                temp_name = handle.name
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp_name, os.fspath(target))
            except OSError:
                if target.exists():
                    return False
                raise
            try:
                dir_fd = os.open(os.fspath(target.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
            return True
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
