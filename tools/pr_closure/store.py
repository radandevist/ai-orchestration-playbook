from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional, Tuple

from pr_closure.contract import (
    COMMIT_ID_PATTERN,
    command_sequence_digest,
    legacy_command_sequence_digest,
)
from pr_closure.model import ClosureState


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


class LifecycleTransitionError(StoreError):
    """Raised when a lifecycle writer is used outside its required source state."""


COMMIT_EVENT = "commit"
VERIFICATION_EVENT = "verification"
REVIEW_EVENT = "review"
REPAIR_STRATEGY_EVENT = "REPAIR_STRATEGY"
REVIEW_DISPATCH_EVENT = "REVIEW_DISPATCH"

LIFECYCLE_EVENT_TYPES = frozenset(
    (REPAIR_STRATEGY_EVENT, REVIEW_DISPATCH_EVENT)
)

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

_RESERVED_EVENT_KEYS = frozenset(
    _REQUIRED_EVENT_KEYS + ("content_digest", "config_digest")
)

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
        REPAIR_STRATEGY_EVENT,
        REVIEW_DISPATCH_EVENT,
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


def _require_attempt_id(value) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise MalformedEvidence(
            "attempt_id must be a 64-character lowercase hexadecimal string"
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


def _require_lifecycle_text(value, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise MalformedEvidence(
            f"{label} must be a non-empty string without surrounding whitespace"
        )
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
      verification/<commit>/<config-digest>/<attempt-id>.json
                                      append-only attempt records (T6L-F2)
      reviews/<commit>/<review-id>.json  atomic review records

    Verification attempts are append-only: each attempt carries an immutable
    config identity (the digest of the exact ordered command sequence) and
    lives at its own attempt-id path, so multiple attempts and configurations
    at the same commit coexist without overwriting one another.
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

    def verification_dir(self, commit) -> Path:
        return self._base / "verification" / _require_commit(commit)

    def verification_path(self, commit, config_digest, attempt_id) -> Path:
        return (
            self.verification_dir(commit)
            / _require_digest(config_digest)
            / f"{_require_attempt_id(attempt_id)}.json"
        )

    def verification_paths(self, commit) -> Tuple[Path, ...]:
        """Every stored verification attempt for a commit, in stable order.

        Enumerates the tree strictly: every entry under the commit directory
        must conform to the exact ``<config-digest>/<attempt-id>.json``
        grammar. A legacy flat file, a non-directory entry where a config
        directory is required, a config-directory name that is not an exact
        lowercase 64-hex digest, a direct child that is not an exact safe
        attempt JSON filename, or a nested directory/symlink shape raises
        :class:`MalformedEvidence` instead of being skipped (T6L-F6). Valid
        multiple config/attempt trees remain accepted.

        The ``verification`` parent is validated first with lstat/no-follow
        semantics (T6LC-F10): a genuinely absent parent chain means no evidence
        and returns ``()``, while a present regular file, symlink (dangling or
        pointing to any external target), FIFO/device/socket, or any other
        non-real-directory parent raises :class:`MalformedEvidence`. The commit
        root is then validated the same way: a genuinely nonexistent
        ``verification/<commit>`` means no evidence and returns ``()``, while a
        present regular file, symlink, FIFO/device/socket, or any other
        non-real-directory root raises :class:`MalformedEvidence` instead of
        being treated as absent. Filesystem shape or race errors while
        validating or enumerating fail closed as :class:`MalformedEvidence`.
        """
        parent = self._base / "verification"
        directory = self.verification_dir(commit)
        try:
            parent_entry = parent.lstat()
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise MalformedEvidence(
                "cannot validate verification parent: {0}".format(parent)
            ) from error
        if not stat.S_ISDIR(parent_entry.st_mode):
            raise MalformedEvidence(
                "verification parent must be a real directory: {0}".format(parent)
            )
        try:
            entry = directory.lstat()
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise MalformedEvidence(
                "cannot validate verification commit root: {0}".format(directory)
            ) from error
        if not stat.S_ISDIR(entry.st_mode):
            raise MalformedEvidence(
                "verification commit root must be a real directory: {0}".format(
                    directory
                )
            )
        try:
            paths = []
            for digest_dir in sorted(directory.iterdir()):
                if _DIGEST_RE.fullmatch(digest_dir.name) is None:
                    raise MalformedEvidence(
                        "verification config directory must be an exact lowercase "
                        "64-hex digest: {0}".format(digest_dir)
                    )
                if digest_dir.is_symlink() or not digest_dir.is_dir():
                    raise MalformedEvidence(
                        "verification config directory must be a real directory: {0}".format(
                            digest_dir
                        )
                    )
                for path in sorted(digest_dir.iterdir()):
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or not path.name.endswith(".json")
                        or _DIGEST_RE.fullmatch(path.stem) is None
                    ):
                        raise MalformedEvidence(
                            "verification attempt must be a direct <attempt-id>.json "
                            "file: {0}".format(path)
                        )
                    paths.append(path)
            return tuple(paths)
        except OSError as error:
            raise MalformedEvidence(
                "cannot enumerate verification tree: {0}".format(directory)
            ) from error

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
        config_digest: Optional[str] = None,
    ) -> dict:
        """Append one newline-delimited JSON event without touching prior bytes.

        Lifecycle events are deliberately excluded from this generic seam.
        Callers must use the state-checked lifecycle writers instead.

        Verification and review artifact events must bind the exact artifact
        bytes through ``content_digest`` (C6D-F3); verification artifact
        events additionally bind the immutable ``config_digest`` config
        identity (T6L-F2). The only exception is a FAILED verification event,
        which is itself the evidence and binds the append-only events file.
        ``content_digest`` and ``config_digest`` are envelope-owned and can
        never be injected through ``payload``.
        """
        if event_type in LIFECYCLE_EVENT_TYPES:
            raise LifecycleTransitionError(
                "{0} must be recorded through its state-checked lifecycle writer".format(
                    event_type
                )
            )
        return self._append_event(
            event_type,
            commit,
            evidence_path,
            payload=payload,
            content_digest=content_digest,
            config_digest=config_digest,
        )

    def _append_event(
        self,
        event_type,
        commit,
        evidence_path,
        payload: Optional[Mapping] = None,
        content_digest: Optional[str] = None,
        config_digest: Optional[str] = None,
    ) -> dict:
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
        if config_digest is not None:
            config_digest = _require_digest(config_digest)
            if event_type != VERIFICATION_EVENT or is_failed_verification:
                raise MalformedEvidence(
                    "config_digest applies only to verification artifact events"
                )
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
        if config_digest is not None:
            event["config_digest"] = config_digest
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

    def record_review_dispatch(
        self,
        commit,
        *,
        lane_id: str,
        current_state: ClosureState,
    ) -> dict:
        """Record review ownership from the exact REVIEW_READY source state."""
        self._require_lifecycle_source_state(
            REVIEW_DISPATCH_EVENT,
            current_state,
            ClosureState.REVIEW_READY,
        )
        lane_id = _require_lifecycle_text(lane_id, "lane_id")
        return self._append_event(
            REVIEW_DISPATCH_EVENT,
            commit,
            str(self.events_path),
            payload={"lane_id": lane_id},
        )

    def record_repair_strategy(
        self,
        commit,
        *,
        root_cause: str,
        strategy: str,
        lane_id: str,
        current_state: ClosureState,
    ) -> dict:
        """Record a repair lane without allowing the design-reset latch to reopen."""
        self._require_lifecycle_source_state(
            REPAIR_STRATEGY_EVENT,
            current_state,
            ClosureState.CHANGES_REQUIRED,
        )
        root_cause = _require_lifecycle_text(root_cause, "root_cause")
        strategy = _require_lifecycle_text(strategy, "strategy")
        lane_id = _require_lifecycle_text(lane_id, "lane_id")

        prior_strategies = set()
        for event in self.read_events():
            if event["event_type"] != REPAIR_STRATEGY_EVENT:
                continue
            prior_root_cause = _require_lifecycle_text(
                event.get("root_cause"),
                "stored REPAIR_STRATEGY root_cause",
            )
            if prior_root_cause != root_cause:
                continue
            prior_strategies.add(
                _require_lifecycle_text(
                    event.get("strategy"),
                    "stored REPAIR_STRATEGY strategy",
                )
            )
        if len(prior_strategies) >= 2:
            raise LifecycleTransitionError(
                "REPAIR_STRATEGY denied: DESIGN_RESET is terminal for root cause {0!r}".format(
                    root_cause
                )
            )

        return self._append_event(
            REPAIR_STRATEGY_EVENT,
            commit,
            str(self.events_path),
            payload={
                "root_cause": root_cause,
                "strategy": strategy,
                "lane_id": lane_id,
            },
        )

    @staticmethod
    def _require_lifecycle_source_state(
        event_type: str,
        current_state: ClosureState,
        required_state: ClosureState,
    ) -> None:
        if not isinstance(current_state, ClosureState):
            raise LifecycleTransitionError(
                "{0} current_state must be a ClosureState".format(event_type)
            )
        if current_state is not required_state:
            raise LifecycleTransitionError(
                "{0} denied: current state {1} is not {2}".format(
                    event_type,
                    current_state.value,
                    required_state.value,
                )
            )

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
            if event_type == VERIFICATION_EVENT and not is_failed_verification:
                config_digest = event.get("config_digest")
                if (
                    not isinstance(config_digest, str)
                    or _DIGEST_RE.fullmatch(config_digest) is None
                ):
                    fail("verification artifact event must bind a config_digest")
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

    def write_verification(self, commit, config_digest, record: Mapping) -> Path:
        """Atomically no-replace one append-only verification attempt.

        The attempt is stored at
        ``verification/<commit>/<config_digest>/<attempt-id>.json`` where the
        attempt id is the SHA-256 of the exact record bytes (T6L-F2). Multiple
        attempts and configurations at the same commit coexist; identical
        bytes stay idempotent with a single event. Different bytes never
        overwrite an existing path.
        """
        commit = _require_commit(commit)
        config_digest = _require_digest(config_digest)
        record = _require_json_object(record, "verification record")
        if "commit" in record and record["commit"] != commit:
            raise MalformedEvidence(
                "verification record commit contradicts its store path"
            )
        if record.get("config_digest") != config_digest:
            raise MalformedEvidence(
                "verification record config identity contradicts the store path"
            )
        line = _serialize(record)
        attempt_id = hashlib.sha256(line).hexdigest()
        target = self.verification_path(commit, config_digest, attempt_id)
        return self._commit_record(
            target, line, VERIFICATION_EVENT, commit, config_digest=config_digest
        )

    def verification_exists(self, commit) -> bool:
        return bool(self.verification_paths(commit))

    def read_verification(self, commit, config_digest, attempt_id) -> dict:
        return _read_json_object(
            self.verification_path(commit, config_digest, attempt_id),
            "verification record",
        )

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

    # -- event/artifact relation (T6L-F5) ---------------------------------

    VERIFICATION_RECORD_PHASES = frozenset({"local_review_ready", "closure_acceptance"})

    def validate_event_relations(self, commit) -> None:
        """Validate the complete event/artifact relation for one commit.

        Fails closed in both directions before any bound read derives
        authority (T6L-F5): every relevant artifact must have exactly one
        compatible binding (identical duplicates allowed; missing, conflicting,
        or legacy path-only bindings rejected), and every artifact event must
        point to an existing allowed target whose descriptor-pinned bytes
        match the bound content digest and whose payload is compatible.
        FAILED verification events are non-artifact attempt evidence with a
        strict shape bound to the events file; they can neither create nor
        supersede green authority.
        """
        commit = _require_commit(commit)
        events = self.read_events()
        commit_events = [event for event in events if event["commit"] == commit]

        verification_by_path = {}
        review_by_path = {}
        for event in commit_events:
            if event["event_type"] == VERIFICATION_EVENT:
                if event.get("outcome") == "FAILED":
                    self._require_failed_verification_event(event)
                    continue
                if event.get("content_digest") is None:
                    raise MalformedEvidence(
                        "verification artifact event must bind a content_digest"
                    )
                config_digest = event.get("config_digest")
                if config_digest is None:
                    raise MalformedEvidence(
                        "verification artifact event must bind a config_digest"
                    )
                self._require_artifact_target(
                    event, commit, "verification", config_digest=config_digest
                )
                verification_by_path.setdefault(event["evidence_path"], []).append(event)
            elif event["event_type"] == REVIEW_EVENT:
                if event.get("content_digest") is None:
                    raise MalformedEvidence(
                        "review artifact event must bind a content_digest"
                    )
                self._require_artifact_target(event, commit, "reviews")
                review_by_path.setdefault(event["evidence_path"], []).append(event)

        for path in self.verification_paths(commit):
            self._read_bound_bytes(path, "verification record")
            bindings = verification_by_path.get(_resolve(os.fspath(path)), ())
            if not bindings:
                raise MalformedEvidence(
                    "verification artifact is not bound by any VERIFICATION_EVENT: {0}".format(
                        path
                    )
                )
            self._require_single_compatible_binding(bindings, path)
        for path in self.review_paths(commit):
            self._read_bound_bytes(path, "review record")
            bindings = review_by_path.get(_resolve(os.fspath(path)), ())
            if not bindings:
                raise MalformedEvidence(
                    "review artifact is not bound by any REVIEW_EVENT: {0}".format(path)
                )
            self._require_single_compatible_binding(bindings, path)

    def _require_single_compatible_binding(self, bindings, path) -> None:
        identities = {
            (
                event["event_type"],
                _resolve(event["evidence_path"]),
                event.get("content_digest"),
                event.get("config_digest"),
            )
            for event in bindings
        }
        if len(identities) != 1:
            raise MalformedEvidence(
                "artifact has conflicting or duplicate incompatible bindings: {0}".format(
                    path
                )
            )

    def _require_artifact_target(
        self, event, commit, kind, config_digest: Optional[str] = None
    ) -> dict:
        """Require an artifact event to point to an existing allowed target.

        The target must live at the exact ``<kind>/<commit>[/<config_digest>]``
        directory, be a direct JSON file, and its descriptor-pinned bytes must
        hash to the bound content digest. The parsed record must be compatible
        with the event: same commit, and for verification artifacts the same
        config identity, outcome PASSED, and a commands sequence whose own
        identity matches.
        """
        path = event["evidence_path"]
        resolved = _resolve(path)
        base = _resolve(os.fspath(self._base))
        expected_dir = os.path.join(base, kind, commit)
        if config_digest is not None:
            expected_dir = os.path.join(expected_dir, config_digest)
        if not (resolved == expected_dir or resolved.startswith(expected_dir + os.sep)):
            raise MalformedEvidence(
                "{0} artifact event target is not an allowed {1} path: {2}".format(
                    kind, kind, path
                )
            )
        rel = os.path.relpath(resolved, expected_dir)
        if not rel.endswith(".json") or os.sep in rel or rel in (".", ".."):
            raise MalformedEvidence(
                "{0} artifact event target must be a direct JSON file: {1}".format(
                    kind, path
                )
            )
        target = Path(resolved)
        raw, digest = self._read_bound_bytes(target, f"{kind} record")
        if digest != event.get("content_digest"):
            raise MalformedEvidence(
                "{0} artifact event content_digest does not match the target bytes: {1}".format(
                    kind, path
                )
            )
        record = _parse_json_bytes(raw, target, f"{kind} record")
        commit_field = "commit" if kind == "verification" else "reviewed_commit"
        if record.get(commit_field) != commit:
            raise MalformedEvidence(
                "{0} record commit contradicts the bound event commit: {1}".format(
                    kind, path
                )
            )
        if kind == "verification":
            if record.get("config_digest") != config_digest:
                raise MalformedEvidence(
                    "verification record config identity contradicts the bound event: {0}".format(
                        path
                    )
                )
            if record.get("outcome") != "PASSED":
                raise MalformedEvidence(
                    "verification artifact must declare outcome PASSED: {0}".format(path)
                )
            sequence = self._require_command_sequence(record, target)
            if "verification_command_timeout_seconds" not in record:
                if config_digest != legacy_command_sequence_digest(sequence):
                    raise MalformedEvidence(
                        "verification record commands contradict its config identity: {0}".format(
                            path
                        )
                    )
                return record
            configured_timeout = record["verification_command_timeout_seconds"]
            if isinstance(configured_timeout, bool) or not isinstance(
                configured_timeout, int
            ):
                raise MalformedEvidence(
                    "verification record missing or invalid timeout: {0}".format(path)
                )
            if configured_timeout < 1:
                raise MalformedEvidence(
                    "verification record timeout must be positive: {0}".format(path)
                )
            if (
                command_sequence_digest(sequence, configured_timeout) != config_digest
            ):
                raise MalformedEvidence(
                    "verification record commands contradict its config identity: {0}".format(
                        path
                    )
                )
        return record

    def _require_failed_verification_event(self, event) -> None:
        """Strict shape for FAILED verification attempt events (T6L-F5).

        A FAILED event is non-artifact attempt evidence bound to the
        append-only events file. It must carry a non-empty commands array of
        structurally valid attempt entries and a ``failed_command_digest``
        naming one of them. It can never create or supersede green authority.
        """
        if event.get("outcome") != "FAILED":
            raise MalformedEvidence("FAILED verification event must declare outcome FAILED")
        if _resolve(event["evidence_path"]) != _resolve(os.fspath(self.events_path)):
            raise MalformedEvidence(
                "FAILED verification event must bind the append-only events file"
            )
        commands = event.get("commands")
        if not isinstance(commands, list) or not commands:
            raise MalformedEvidence(
                "FAILED verification event must carry a non-empty commands array"
            )
        digests = []
        for index, command in enumerate(commands, start=1):
            if not isinstance(command, dict):
                raise MalformedEvidence(
                    "FAILED verification event commands must be objects"
                )
            phase = command.get("phase")
            if not isinstance(phase, str) or phase not in self.VERIFICATION_RECORD_PHASES:
                raise MalformedEvidence(
                    "FAILED verification event command declares unknown phase {0!r}".format(
                        phase
                    )
                )
            digest = command.get("command_digest")
            if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
                raise MalformedEvidence(
                    "FAILED verification event command must carry a valid command_digest"
                )
            label = command.get("command_label")
            if not isinstance(label, str) or not label.strip():
                raise MalformedEvidence(
                    "FAILED verification event command must carry a command_label"
                )
            for key in ("started_at", "ended_at"):
                stamp = command.get(key)
                if not isinstance(stamp, str) or not stamp.strip():
                    raise MalformedEvidence(
                        "FAILED verification event command must carry {0}".format(key)
                    )
            status = command.get("exit_status")
            if not isinstance(status, int) or isinstance(status, bool):
                raise MalformedEvidence(
                    "FAILED verification event command must carry an integer exit_status"
                )
            digests.append(digest)
        failed_digest = event.get("failed_command_digest")
        if (
            not isinstance(failed_digest, str)
            or _DIGEST_RE.fullmatch(failed_digest) is None
        ):
            raise MalformedEvidence(
                "FAILED verification event must carry a valid failed_command_digest"
            )
        if failed_digest not in digests:
            raise MalformedEvidence(
                "FAILED verification event failed_command_digest must name a command"
            )

    # -- event-bound reads (C6C-F4, T6L-F4) -------------------------------

    _O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

    def bound_verification(
        self,
        commit,
        expected_commands: Optional[Tuple[Tuple[str, str], ...]] = None,
        expected_verification_command_timeout_seconds: int = 300,
    ) -> Optional[dict]:
        """Return the event-bound PASSED verification record for the exact
        expected ordered command sequence, or None.

        Authority is derived from descriptor-pinned artifact bytes whose
        SHA-256 digest and config identity a ``VERIFICATION_EVENT`` binds to
        the queried commit. The complete event/artifact relation is validated
        before selection (T6L-F5); a same-path swap during the read window, a
        symlink or multi-link entry, an orphan artifact, a missing or
        conflicting event target, a legacy path-only event, or an incompatible
        payload fails closed with :class:`MalformedEvidence`.

        Passing evidence is selected only for the current exact ordered
        command sequence (C6D-F2): a changed config identity makes old
        attempts non-selecting without erasing them, and a later FAILED
        attempt never supersedes a valid earlier pass (T6L-F2). When several
        passes share the expected identity, the most recent attempt is
        returned.
        """
        commit = _require_commit(commit)
        expected = None
        if expected_commands is not None:
            expected_commands = tuple(expected_commands)
            for phase, digest in expected_commands:
                if phase not in self.VERIFICATION_RECORD_PHASES:
                    raise MalformedEvidence(
                        "expected command sequence carries unknown phase {0!r}".format(phase)
                    )
                _require_digest(digest)
            expected = command_sequence_digest(
                expected_commands, expected_verification_command_timeout_seconds
            )
        self.validate_event_relations(commit)
        events = self.read_events()
        best = None
        best_timestamp = None
        for path in self.verification_paths(commit):
            raw, digest = self._read_bound_bytes(path, "verification record")
            record = _parse_json_bytes(raw, path, "verification record")
            sequence = self._require_command_sequence(record, path)
            if expected is not None:
                if record.get("config_digest") != expected or sequence != expected_commands:
                    continue
            else:
                phases = {phase for phase, _ in sequence}
                if not all(phase in phases for phase in self.VERIFICATION_RECORD_PHASES):
                    raise MalformedEvidence(
                        "PASSED verification record must prove both gate phases: {0}".format(
                            path
                        )
                    )
            timestamp = None
            for event in events:
                if (
                    event["event_type"] == VERIFICATION_EVENT
                    and event["commit"] == commit
                    and _resolve(event["evidence_path"]) == _resolve(os.fspath(path))
                    and event.get("content_digest") == digest
                ):
                    timestamp = event["timestamp"]
                    break
            if best is None or (
                timestamp is not None
                and (best_timestamp is None or timestamp > best_timestamp)
            ):
                best = record
                best_timestamp = timestamp
        return best

    def bound_reviews(self, commit) -> Tuple[Tuple[str, dict], ...]:
        """Return every event-bound review record as ``(review_id, record)``.

        Authority is derived from descriptor-pinned artifact bytes whose
        SHA-256 digest a ``REVIEW_EVENT`` binds to the queried commit. The
        complete event/artifact relation is validated first (T6L-F5): any
        orphan artifact, missing or conflicting event target, legacy path-only
        event, same-path replacement with different bytes, or symlink fails
        closed with :class:`MalformedEvidence`.
        """
        commit = _require_commit(commit)
        self.validate_event_relations(commit)
        bound = []
        for path in self.review_paths(commit):
            raw, _digest = self._read_bound_bytes(path, "review record")
            bound.append((path.stem, _parse_json_bytes(raw, path, "review record")))
        return tuple(bound)

    def _read_bound_bytes(self, target: Path, label: str) -> Tuple[bytes, str]:
        """Descriptor-pinned no-follow read of one artifact (T6L-F4).

        Opens with ``O_NOFOLLOW``, requires a single-link regular file, reads
        and hashes one byte buffer from the descriptor, then compares the
        descriptor identity with the current directory entry before accepting.
        A same-path swap during the window, a symlink, or a multi-link entry
        raises :class:`MalformedEvidence`. Every descriptor is closed on all
        paths.
        """
        path = os.fspath(target)
        try:
            fd = os.open(path, os.O_RDONLY | self._O_NOFOLLOW)
        except OSError as error:
            raise MalformedEvidence(f"cannot open {label}: {target}: {error}") from error
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise MalformedEvidence(f"{label} is not a regular file: {target}")
            if opened.st_nlink != 1:
                raise MalformedEvidence(
                    f"{label} must be a single-link regular file: {target}"
                )
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            try:
                entry = os.lstat(path)
            except OSError as error:
                raise MalformedEvidence(
                    f"cannot re-stat {label}: {target}: {error}"
                ) from error
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise MalformedEvidence(
                    f"{label} directory entry changed identity: {target}"
                )
            if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
                raise MalformedEvidence(
                    f"{label} path changed identity during read: {target}"
                )
        finally:
            os.close(fd)
        return raw, hashlib.sha256(raw).hexdigest()

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
            if command.get("timed_out") is True:
                raise MalformedEvidence(
                    "PASSED verification command must not have timed out: {0}".format(
                        target
                    )
                )
            sequence.append((phase, digest))
        return tuple(sequence)

    def _has_matching_event(
        self,
        events,
        event_type,
        commit,
        target: Path,
        content_digest: Optional[str] = None,
        config_digest: Optional[str] = None,
    ) -> bool:
        """Whether an event of ``event_type`` binds ``commit`` to the resolved
        artifact path ``target`` currently resolves to.

        When ``content_digest`` is given, the event must also bind those exact
        artifact bytes (C6D-F3): a same-path replacement with different bytes
        or a legacy path-only event never matches. Verification artifact
        events additionally match the immutable ``config_digest`` identity.
        """
        resolved = _resolve(os.fspath(target))
        return any(
            event["event_type"] == event_type
            and event["commit"] == commit
            and event["evidence_path"] == resolved
            and (content_digest is None or event.get("content_digest") == content_digest)
            and (config_digest is None or event.get("config_digest") == config_digest)
            for event in events
        )

    # -- atomic record commit ---------------------------------------------

    def _commit_record(
        self, target: Path, line: bytes, event_type, commit, config_digest: Optional[str] = None
    ) -> Path:
        """Commit a record artifact and its evidence event as one transaction.

        The target is re-resolved against the durable-path rules before any
        mutation, so a swapped or symlinked verification/reviews directory
        cannot create the artifact under a forbidden area and fail only after
        the fact. The artifact is created with an atomic no-replace primitive;
        exactly one different-byte writer wins, and losers compare the
        committed bytes and raise ``EvidenceConflict``. The event is appended
        only after the artifact exists and binds the exact artifact bytes
        through their SHA-256 content digest (C6D-F3); if the append fails,
        the artifact created by this call is rolled back. An identical-byte
        retry repairs a missing event instead of taking an early return, while
        an already-complete write stays idempotent without a duplicate event.
        """
        _require_durable(os.fspath(target), "record path")
        created = self._atomic_create(target, line)
        if not created:
            existing = self._read_existing_for_compare(target)
            if existing != line:
                raise EvidenceConflict(
                    f"record already exists with different bytes: {target}"
                )
        content_digest = hashlib.sha256(line).hexdigest()
        if self._has_matching_event(
            self.read_events(),
            event_type,
            commit,
            target,
            content_digest=content_digest,
            config_digest=config_digest,
        ):
            return target
        try:
            self.append_event(
                event_type,
                commit,
                os.fspath(target),
                content_digest=content_digest,
                config_digest=config_digest,
            )
        except BaseException:
            if created:
                try:
                    os.unlink(os.fspath(target))
                except OSError:
                    pass
            raise
        return target

    def _read_existing_for_compare(self, target: Path) -> bytes:
        """Descriptor-pinned read of a just-created record for byte compare.

        The winning writer creates the target through ``os.link`` and removes
        its temporary link right after; a loser can open the target inside
        that tiny window and observe ``st_nlink == 2``. Retry briefly so a
        transient multi-link window never masks a legitimately created record;
        a persistent violation (symlink, hard-link swap, or multi-link entry)
        still fails closed with :class:`MalformedEvidence`.
        """
        last_error = None
        for attempt in range(5):
            try:
                existing, _ = self._read_bound_bytes(target, "existing record")
                return existing
            except MalformedEvidence as error:
                last_error = error
                if attempt == 4:
                    break
                time.sleep(0.002)
        raise last_error

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
            # Drop the temporary link before the directory fsync so a
            # concurrent loser never observes a multi-link entry for a
            # legitimately created single-link record (T6L-F4).
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            temp_name = None
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
