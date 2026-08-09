from __future__ import annotations

import hashlib
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

_RESERVED_EVENT_KEYS = frozenset(_REQUIRED_EVENT_KEYS + ("content_digest",))

# Event types the store vets as authoritative. Later tasks that add custom event
# types must register them here so both write and read sides accept them.
# INFRA_FAILURE is written by the Task 6 CLI's record-infra-failure command and
# consumed by pr_closure.sources.require_infra_event; its payload carries the
# bounded fields failed_job and test_steps_not_started.
KNOWN_EVENT_TYPES = frozenset(
    (
        COMMIT_EVENT,
        VERIFICATION_EVENT,
        REVIEW_EVENT,
        "STAGNATION_CONFIG",
        "REPAIR_STRATEGY",
        "INFRA_FAILURE",
    )
)

# Non-durable session areas. Evidence rooted here (or anywhere beneath them,
# including through symlinks) is never authoritative.
_FORBIDDEN_ROOT_SPECS = ("/tmp", os.path.expanduser("~/.claude/jobs"))

_COMMIT_ID_RE = re.compile(COMMIT_ID_PATTERN)
_DIGEST_RE = re.compile("^[0-9a-f]{64}$")
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


def _require_digest(value) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise MalformedEvidence(
            "content_digest must be a 64-character lowercase hexadecimal string"
        )
    return value


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


def _parse_json_bytes(raw: bytes, path: Path, label: str) -> dict:
    """Parse a JSON object from the exact bytes an authority check hashed."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MalformedEvidence(f"malformed {label}: {path}: {error}") from error
    return _require_json_object(parsed, f"{label}: {path}")


def _read_json_object(path: Path, label: str) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise MalformedEvidence(f"cannot read {label}: {path}: {error}") from error
    return _parse_json_bytes(raw, path, label)


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

    def append_event(
        self,
        event_type,
        commit,
        evidence_path,
        payload: Optional[Mapping] = None,
        content_digest: Optional[str] = None,
    ) -> dict:
        """Append one newline-delimited JSON event without touching prior bytes.

        Verification and review artifact events must bind the exact artifact
        bytes through ``content_digest`` (C6D-F3); the only exception is a
        FAILED verification event, which is itself the evidence and binds the
        append-only events file. ``content_digest`` is envelope-owned and can
        never be injected through ``payload``.
        """
        if not isinstance(event_type, str) or event_type not in KNOWN_EVENT_TYPES:
            raise MalformedEvidence(
                f"unknown event_type: {event_type!r}; known types: "
                f"{', '.join(sorted(KNOWN_EVENT_TYPES))}"
            )
        commit = _require_commit(commit)
        evidence = _require_durable(evidence_path)
        is_failed_verification = (
            event_type == VERIFICATION_EVENT
            and isinstance(payload, dict)
            and payload.get("outcome") == "FAILED"
        )
        if content_digest is None and not is_failed_verification:
            if event_type in (VERIFICATION_EVENT, REVIEW_EVENT):
                raise MalformedEvidence(
                    f"{event_type} artifact events must bind a content_digest"
                )
        if content_digest is not None:
            content_digest = _require_digest(content_digest)
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "project": self._project,
            "pr": self._pr,
            "commit": commit,
            "event_type": event_type,
            "evidence_path": evidence,
        }
        if content_digest is not None:
            event["content_digest"] = content_digest
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
        digest = event.get("content_digest")
        if event_type in (VERIFICATION_EVENT, REVIEW_EVENT):
            is_failed_verification = (
                event_type == VERIFICATION_EVENT
                and event.get("outcome") == "FAILED"
            )
            if digest is None:
                if not is_failed_verification:
                    fail(
                        f"{event_type} artifact event must bind a content_digest; "
                        "legacy path-only events never carry authority"
                    )
            else:
                try:
                    _require_digest(digest)
                except StoreError:
                    fail(f"malformed content_digest: {digest!r}")
        elif digest is not None:
            try:
                _require_digest(digest)
            except StoreError:
                fail(f"malformed content_digest: {digest!r}")

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

    # -- event-bound reads (C6C-F4) ---------------------------------------

    VERIFICATION_RECORD_PHASES = frozenset({"local_review_ready", "closure_acceptance"})

    def bound_verification(
        self, commit, expected_commands: Optional[Tuple[Tuple[str, str], ...]] = None
    ) -> Optional[dict]:
        """Return the event-bound PASSED verification record, or None.

        Authority is derived from the exact artifact bytes whose SHA-256 digest
        a ``VERIFICATION_EVENT`` binds to the queried commit: the bytes are
        read once, hashed, matched against the event, and parsed from that same
        buffer (no check-then-read race). An artifact with no matching
        digest-event, a legacy path-only event, a same-path regular-file
        replacement with different bytes, a non-PASSED outcome, or a record
        with a malformed command entry fails closed with
        :class:`MalformedEvidence`. A missing artifact never yields bytes.

        ``expected_commands`` is the exact ordered ``(phase, command_digest)``
        sequence the current config requires (C6D-F2). When provided, the
        record must match it exactly; a truncated, reordered, duplicated,
        substituted, or old-config sequence returns None (non-green/stale)
        instead of approval. When omitted, only the structural checks apply
        (both phases present, exact integer zero statuses, valid digests).
        """
        commit = _require_commit(commit)
        if expected_commands is not None:
            expected_commands = tuple(expected_commands)
            for phase, digest in expected_commands:
                if phase not in self.VERIFICATION_RECORD_PHASES:
                    raise MalformedEvidence(
                        "expected command sequence carries unknown phase {0!r}".format(phase)
                    )
                _require_digest(digest)
        target = self.verification_path(commit)
        if not target.is_file():
            return None
        events = self.read_events()
        raw, digest = self._read_bound_bytes(target, "verification record")
        if not self._has_matching_event(
            events, VERIFICATION_EVENT, commit, target, content_digest=digest
        ):
            raise MalformedEvidence(
                "verification artifact bytes are not bound by any VERIFICATION_EVENT: {0}".format(
                    target
                )
            )
        record = _parse_json_bytes(raw, target, "verification record")
        if record.get("commit") != commit:
            raise MalformedEvidence(
                "verification record commit contradicts its queried commit: {0}".format(target)
            )
        if record.get("outcome") != "PASSED":
            raise MalformedEvidence(
                "verification artifact must declare outcome PASSED: {0}".format(target)
            )
        sequence = self._require_command_sequence(record, target)
        if expected_commands is not None:
            if sequence != expected_commands:
                return None
        else:
            phases = {phase for phase, _ in sequence}
            if not all(phase in phases for phase in self.VERIFICATION_RECORD_PHASES):
                raise MalformedEvidence(
                    "PASSED verification record must prove both gate phases: {0}".format(target)
                )
        return record

    def _require_command_sequence(
        self, record: dict, target: Path
    ) -> Tuple[Tuple[str, str], ...]:
        """Structurally validate the commands array of a PASSED record.

        Every entry must declare a known phase, a 64-lowercase-hex
        ``command_digest``, a non-empty human-safe ``command_label``, string
        timestamps, and an exact integer exit status zero (booleans are
        invalid). Returns the ordered ``(phase, command_digest)`` sequence.
        """
        commands = record.get("commands")
        if not isinstance(commands, list) or not commands:
            raise MalformedEvidence(
                "PASSED verification record must carry a non-empty commands array: {0}".format(
                    target
                )
            )
        sequence = []
        for command in commands:
            if not isinstance(command, dict):
                raise MalformedEvidence(
                    "verification record commands must be objects: {0}".format(target)
                )
            phase = command.get("phase")
            if not isinstance(phase, str) or phase not in self.VERIFICATION_RECORD_PHASES:
                raise MalformedEvidence(
                    "verification record command declares unknown phase {0!r}: {1}".format(
                        phase, target
                    )
                )
            digest = command.get("command_digest")
            if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
                raise MalformedEvidence(
                    "verification record command must carry a valid command_digest: {0}".format(
                        target
                    )
                )
            label = command.get("command_label")
            if not isinstance(label, str) or not label.strip():
                raise MalformedEvidence(
                    "verification record command must carry a command_label: {0}".format(target)
                )
            for key in ("started_at", "ended_at"):
                stamp = command.get(key)
                if not isinstance(stamp, str) or not stamp.strip():
                    raise MalformedEvidence(
                        "verification record command must carry {0}: {1}".format(key, target)
                    )
            status = command.get("exit_status")
            if not isinstance(status, int) or isinstance(status, bool) or status != 0:
                raise MalformedEvidence(
                    "PASSED verification command must carry exact integer exit status 0: {0}".format(
                        target
                    )
                )
            sequence.append((phase, digest))
        return tuple(sequence)

    def bound_reviews(self, commit) -> Tuple[Tuple[str, dict], ...]:
        """Return every event-bound review record as ``(review_id, record)``.

        Authority is derived from the exact artifact bytes whose SHA-256 digest
        a ``REVIEW_EVENT`` binds to the queried commit. Any present artifact
        without a matching digest-event (including a same-path regular-file
        replacement with different bytes, a symlink that changes the resolved
        path, or a legacy path-only event) is orphan/conflicting evidence and
        fails closed.
        """
        commit = _require_commit(commit)
        events = self.read_events()
        bound = []
        for path in self.review_paths(commit):
            raw, digest = self._read_bound_bytes(path, "review record")
            if not self._has_matching_event(
                events, REVIEW_EVENT, commit, path, content_digest=digest
            ):
                raise MalformedEvidence(
                    "review artifact bytes are not bound by any REVIEW_EVENT: {0}".format(path)
                )
            bound.append((path.stem, _parse_json_bytes(raw, path, "review record")))
        return tuple(bound)

    def _read_bound_bytes(self, target: Path, label: str) -> Tuple[bytes, str]:
        """Read artifact bytes once and hash them for event binding.

        Returns ``(raw, sha256_hex)`` from one read so authority is always
        derived from the exact bytes whose digest was checked.
        """
        try:
            raw = target.read_bytes()
        except OSError as error:
            raise MalformedEvidence(f"cannot read {label}: {target}: {error}") from error
        return raw, hashlib.sha256(raw).hexdigest()

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
            raw, digest = self._read_bound_bytes(verification, "verification record")
            if not self._has_matching_event(
                events, VERIFICATION_EVENT, commit, verification, content_digest=digest
            ):
                return ApprovalState.UNVERIFIED
            record = _parse_json_bytes(raw, verification, "verification record")
            self._require_command_sequence(record, verification)
            verification_ok = True

        review_ok = False
        for path in self.review_paths(commit):
            raw, digest = self._read_bound_bytes(path, "review record")
            if not self._has_matching_event(
                events, REVIEW_EVENT, commit, path, content_digest=digest
            ):
                continue
            _parse_json_bytes(raw, path, "review record")
            review_ok = True
            break

        if not verification_ok or not review_ok:
            return ApprovalState.UNVERIFIED
        if tip is None:
            return ApprovalState.UNVERIFIED
        if tip != commit:
            return ApprovalState.STALE
        return ApprovalState.APPROVED

    def _has_matching_event(
        self,
        events,
        event_type,
        commit,
        target: Path,
        content_digest: Optional[str] = None,
    ) -> bool:
        """Whether an event of ``event_type`` binds ``commit`` to the resolved
        artifact path ``target`` currently resolves to.

        When ``content_digest`` is given, the event must also bind those exact
        artifact bytes (C6D-F3): a same-path replacement with different bytes
        or a legacy path-only event never matches.
        """
        resolved = _resolve(os.fspath(target))
        return any(
            event["event_type"] == event_type
            and event["commit"] == commit
            and event["evidence_path"] == resolved
            and (content_digest is None or event.get("content_digest") == content_digest)
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
        the artifact exists and binds the exact artifact bytes through their
        SHA-256 content digest (C6D-F3); if the append fails, the artifact
        created by this call is rolled back. An identical-byte retry repairs a
        missing event instead of taking an early return, while an
        already-complete write stays idempotent without a duplicate event.
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
        content_digest = hashlib.sha256(line).hexdigest()
        if self._has_matching_event(
            self.read_events(), event_type, commit, target, content_digest=content_digest
        ):
            return target
        try:
            self.append_event(
                event_type, commit, os.fspath(target), content_digest=content_digest
            )
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
