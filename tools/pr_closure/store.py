from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional, Tuple

from pr_closure.contract import (
    COMMIT_ID_PATTERN,
    command_sequence_digest,
    legacy_command_sequence_digest,
)
from pr_closure.model import ClosureState
from pr_closure.jsonio import StrictJsonError, loads as strict_json_loads
from pr_closure.secure_paths import (
    SecurePathError,
    append_contained_file as secure_append_contained_file,
    atomic_create as secure_atomic_create,
    ensure_directory,
    read_contained_file,
    rename_no_replace,
)


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
POLICY_ADOPTION_EVENT = "policy_adoption"

LIFECYCLE_EVENT_TYPES = frozenset(
    (REPAIR_STRATEGY_EVENT, REVIEW_DISPATCH_EVENT, POLICY_ADOPTION_EVENT)
)
RETIREMENT_PREPARED_EVENT = "review_retirement_prepared"
RETIREMENT_COPIED_EVENT = "review_retirement_copied"
RETIREMENT_COMMITTED_EVENT = "review_retirement_committed"
RETIREMENT_FINALIZED_EVENT = "review_retirement_finalized"
POLICY_ACTIVATION_EVENT = "policy_activation"

RETIREMENT_EVENT_TYPES = (
    RETIREMENT_PREPARED_EVENT,
    RETIREMENT_COPIED_EVENT,
    RETIREMENT_COMMITTED_EVENT,
    RETIREMENT_FINALIZED_EVENT,
)
RETIREMENT_STATES = ("ACTIVE", "PREPARED", "COPIED", "COMMITTED", "FINALIZED")
RETIREMENT_REASONS = frozenset(
    (
        "policy-migration: claude-reviewer-forbidden",
        "policy-migration: schema-v2-provenance-required",
        "policy-rollback: same-family-review-replaced",
    )
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
        *RETIREMENT_EVENT_TYPES,
        POLICY_ACTIVATION_EVENT,
        POLICY_ADOPTION_EVENT,
    )
)

# Non-durable session areas. Evidence rooted here (or anywhere beneath them,
# including through symlinks) is never authoritative.
_FORBIDDEN_ROOT_SPECS = ("/tmp", os.path.expanduser("~/.claude/jobs"))

_COMMIT_ID_RE = re.compile(COMMIT_ID_PATTERN)
_DIGEST_RE = re.compile("^[0-9a-f]{64}$")
_UNSAFE_COMPONENT_RE = re.compile(r"[/\\\x00]")
_RETIREMENT_ENVELOPE_KEYS = frozenset(
    (
        "schema_version",
        "operation",
        "retirement_id",
        "repository",
        "project",
        "pr_number",
        "commit",
        "review_id",
        "source_path",
        "expected_sha256",
        "reason",
        "policy_id",
        "requested_at",
    )
)
_RETIREMENT_EVENT_KEYS = frozenset(
    (
        "event_id",
        "operation",
        "retirement_id",
        "repository",
        "review_id",
        "source_path",
        "staging_path",
        "final_path",
        "expected_sha256",
        "reason",
        "policy_id",
    )
)
_POLICY_ADOPTION_EVENT_KEYS = frozenset(
    (
        "event_id",
        "transition_kind",
        "activation_event_id",
        "repository",
        "policy_id",
        "policy_digest",
        "mode",
        "staged_config_digest",
        "enforced_config_digest",
        "previous_transition_id",
        "previous_policy_id",
        "previous_policy_digest",
        "previous_enforced_config_digest",
    )
)

_STREAM_ANCHOR_IDENTITY_KEYS = frozenset(
    (
        "schema_version",
        "project",
        "pr",
        "stream_id",
        "events_path",
        "events_device",
        "events_inode",
        "initial_size",
        "initial_sha256",
        "initial_event_count",
    )
)
_STREAM_ANCHOR_HEAD_KEYS = frozenset(
    (
        "schema_version",
        "record_type",
        "phase",
        "project",
        "pr",
        "stream_id",
        "operation_id",
        "anchor_device",
        "anchor_inode",
        "identity_device",
        "identity_inode",
        "head_device",
        "head_inode",
        "events_device",
        "events_inode",
        "previous_size",
        "previous_sha256",
        "previous_event_count",
        "new_size",
        "new_sha256",
        "new_event_count",
        "line_size",
        "line_sha256",
    )
)
_STREAM_ANCHOR_PHASES = frozenset(("PREPARE", "COMMIT", "ABORT"))
_EMPTY_STREAM_DIGEST = hashlib.sha256(b"").hexdigest()
_EVENT_STORE_OPERATIONAL_ENTRIES = frozenset(
    (
        "verification",
        "reviews",
        "retirements",
        "reviews-retirement-staging",
        "reviews-retired",
        ".policy-adoption.lock",
        "heavy-job.lock",
        "heavy-job.meta.json",
    )
)

_PROJECT_LOCK_STATE = threading.local()
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
        parsed = strict_json_loads(raw, label)
    except StrictJsonError as error:
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

    @property
    def stream_anchor_dir(self) -> Path:
        """External append-integrity and crash-recovery directory."""
        return self._root / ".event-stream-anchors" / self._project / str(self._pr)

    @property
    def stream_anchor_identity_path(self) -> Path:
        return self.stream_anchor_dir / "identity.json"

    @property
    def stream_anchor_head_path(self) -> Path:
        return self.stream_anchor_dir / "head.jsonl"

    @contextmanager
    def _policy_adoption_lock(self, *, exclusive=True):
        """Serialize project-wide policy adoption reads and writes.

        The project directory itself is the stable lock inode.  Locking it
        avoids creating a marker during read-only status/import inspection,
        while still serializing the first adoption against every other PR in
        the project.
        """
        key = (os.fspath(self._root), self._project)
        held_locks = getattr(_PROJECT_LOCK_STATE, "held", None)
        if held_locks is None:
            held_locks = {}
            _PROJECT_LOCK_STATE.held = held_locks
        held = held_locks.get(key)
        if held is not None:
            if exclusive and not held["exclusive"]:
                raise MalformedEvidence(
                    "cannot upgrade a shared project lock to exclusive"
                )
            held["depth"] += 1
            try:
                yield True
            finally:
                held["depth"] -= 1
            return

        project_dir = self._root / self._project
        fd = None
        acquired = False
        missing = False
        try:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise MalformedEvidence(
                    "platform does not provide O_NOFOLLOW for policy adoption lock"
                )
            if exclusive:
                ensure_directory(project_dir)
            else:
                try:
                    project_entry = project_dir.lstat()
                except FileNotFoundError:
                    missing = True
                except OSError as error:
                    raise MalformedEvidence(
                        "cannot validate policy adoption root: {0}".format(project_dir)
                    ) from error
                if not missing and (
                    not stat.S_ISDIR(project_entry.st_mode) or project_dir.is_symlink()
                ):
                    raise MalformedEvidence(
                        "policy adoption root must be a real directory: {0}".format(
                            project_dir
                        )
                    )
            if not missing:
                flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                fd = os.open(os.fspath(project_dir), flags)
                entry = os.fstat(fd)
                if not stat.S_ISDIR(entry.st_mode):
                    raise MalformedEvidence(
                        "policy adoption lock root must be a real directory: {0}".format(
                            project_dir
                        )
                    )
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                held_locks[key] = {
                    "fd": fd,
                    "exclusive": exclusive,
                    "depth": 1,
                }
                acquired = True
        except MalformedEvidence:
            if fd is not None:
                os.close(fd)
            raise
        except SecurePathError as error:
            if fd is not None:
                os.close(fd)
            raise MalformedEvidence(
                "cannot establish policy adoption lock: {0}".format(project_dir)
            ) from error
        except OSError as error:
            if fd is not None:
                os.close(fd)
            raise MalformedEvidence(
                "cannot establish policy adoption lock: {0}".format(project_dir)
            ) from error
        if missing:
            yield False
            return
        try:
            yield True
        finally:
            held_locks.pop(key, None)
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass

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
        for event in self._read_authoritative_events():
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

    def record_policy_activation(self, commit, staged_config_digest, projected_config_digest) -> dict:
        commit = _require_commit(commit)
        staged_config_digest = _require_digest(staged_config_digest)
        projected_config_digest = _require_digest(projected_config_digest)
        event_id = hashlib.sha256(
            (commit + staged_config_digest + projected_config_digest).encode("ascii")
        ).hexdigest()
        payload = {
            "event_id": event_id,
            "result": "ELIGIBLE",
            "staged_config_digest": staged_config_digest,
            "projected_enforced_config_digest": projected_config_digest,
        }
        for event in self._read_authoritative_events():
            if event["event_type"] != POLICY_ACTIVATION_EVENT or event["commit"] != commit:
                continue
            if all(event.get(key) == value for key, value in payload.items()):
                return event
        return self.append_event(
            POLICY_ACTIVATION_EVENT,
            commit,
            os.fspath(self.events_path),
            payload=payload,
        )

    def matching_policy_activation(
        self, commit, staged_config_digest, projected_config_digest
    ) -> Optional[dict]:
        commit = _require_commit(commit)
        staged_config_digest = _require_digest(staged_config_digest)
        projected_config_digest = _require_digest(projected_config_digest)
        expected_id = hashlib.sha256(
            (commit + staged_config_digest + projected_config_digest).encode("ascii")
        ).hexdigest()
        matches = []
        for event in self._read_authoritative_events():
            if (
                event["event_type"] == POLICY_ACTIVATION_EVENT
                and event["commit"] == commit
                and event.get("event_id") == expected_id
                and event.get("result") == "ELIGIBLE"
                and event.get("staged_config_digest") == staged_config_digest
                and event.get("projected_enforced_config_digest") == projected_config_digest
            ):
                matches.append(event)
        if len(matches) > 1:
            raise MalformedEvidence("policy activation identity is duplicated")
        return matches[0] if matches else None

    def policy_activations_for_projected(self, commit, projected_config_digest) -> list:
        commit = _require_commit(commit)
        projected_config_digest = _require_digest(projected_config_digest)
        matches = []
        for event in self._read_authoritative_events():
            if (
                event["event_type"] != POLICY_ACTIVATION_EVENT
                or event["commit"] != commit
                or event.get("result") != "ELIGIBLE"
                or event.get("projected_enforced_config_digest") != projected_config_digest
            ):
                continue
            staged_digest = event.get("staged_config_digest")
            _require_digest(staged_digest)
            expected_id = hashlib.sha256(
                (commit + staged_digest + projected_config_digest).encode("ascii")
            ).hexdigest()
            if event.get("event_id") != expected_id:
                raise MalformedEvidence("policy activation event id is not bound to its digests")
            matches.append(event)
        return matches

    @staticmethod
    def _policy_adoption_event_id(identity: Mapping) -> str:
        raw = json.dumps(
            dict(identity), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _require_policy_adoption_event(
        self, event: dict, number: int, path: Path
    ) -> None:
        def fail(detail: str) -> None:
            raise MalformedEvidence(
                f"policy adoption event at line {number}: {path}: {detail}"
            )

        custom = {key: event.get(key) for key in _POLICY_ADOPTION_EVENT_KEYS}
        actual_custom = frozenset(
            key for key in event if key not in _REQUIRED_EVENT_KEYS
        )
        if actual_custom != _POLICY_ADOPTION_EVENT_KEYS:
            fail(
                "keys differ: missing={0}, unknown={1}".format(
                    sorted(_POLICY_ADOPTION_EVENT_KEYS - actual_custom),
                    sorted(actual_custom - _POLICY_ADOPTION_EVENT_KEYS),
                )
            )
        if event.get("evidence_path") != _resolve(os.fspath(path)):
            fail("evidence_path must be the exact events file")
        if event.get("mode") != "enforced":
            fail("mode must be enforced")
        transition_kind = event.get("transition_kind")
        if transition_kind not in (
            "initial-adoption",
            "continued-adoption",
            "authorized-rollback",
        ):
            fail("transition_kind is not an authorized adoption transition")
        try:
            _require_digest(event.get("activation_event_id"))
            _require_lifecycle_text(event.get("repository"), "repository")
            _require_component(event.get("policy_id"), "policy_id")
            _require_digest(event.get("policy_digest"))
            _require_digest(event.get("staged_config_digest"))
            _require_digest(event.get("enforced_config_digest"))
            _require_digest(event.get("event_id"))
        except StoreError as error:
            fail(str(error))
        if event.get("repository") == "":
            fail("repository must be non-blank")
        previous_id = event.get("previous_transition_id")
        previous_policy_id = event.get("previous_policy_id")
        previous_policy_digest = event.get("previous_policy_digest")
        previous_enforced_digest = event.get("previous_enforced_config_digest")
        if transition_kind == "initial-adoption":
            if any(
                value is not None
                for value in (
                    previous_id,
                    previous_policy_id,
                    previous_policy_digest,
                    previous_enforced_digest,
                )
            ):
                fail("initial adoption must not name a predecessor")
        else:
            try:
                _require_digest(previous_id)
                _require_component(previous_policy_id, "previous_policy_id")
                _require_digest(previous_policy_digest)
                _require_digest(previous_enforced_digest)
            except StoreError as error:
                fail(str(error))
        identity = dict(custom)
        identity.pop("event_id")
        identity.update(
            {
                "project": event["project"],
                "pr": event["pr"],
                "commit": event["commit"],
            }
        )
        if self._policy_adoption_event_id(identity) != event.get("event_id"):
            fail("event_id is not bound to the complete transition identity")

    def project_policy_adoption_events(self) -> Tuple[dict, ...]:
        """Read every project-scoped policy adoption transition for audit/migration.

        Adoption is intentionally discovered across all PR event streams. These
        events document lifecycle transitions but are not policy authority for a
        repository with an immutable registry floor.
        """
        with self._policy_adoption_lock(exclusive=False) as acquired:
            if not acquired:
                return ()
            return self._project_policy_adoption_events_unlocked()

    def _external_anchor_pr_numbers(self) -> Tuple[int, ...]:
        root = self._root / ".event-stream-anchors"
        project_dir = root / self._project
        for directory in (root, project_dir):
            try:
                entry = directory.lstat()
            except FileNotFoundError:
                return ()
            except OSError as error:
                raise MalformedEvidence("cannot validate external stream anchors") from error
            if not stat.S_ISDIR(entry.st_mode) or directory.is_symlink():
                raise MalformedEvidence(
                    "external stream anchor parent must be a real directory: {0}".format(
                        directory
                    )
                )
        try:
            children = sorted(project_dir.iterdir(), key=lambda child: child.name)
        except OSError as error:
            raise MalformedEvidence("cannot enumerate external stream anchors") from error
        numbers = []
        for child in children:
            if re.fullmatch(r"[1-9][0-9]*", child.name) is None:
                raise MalformedEvidence(
                    "external stream anchor child must be a positive PR directory: {0}".format(
                        child
                    )
                )
            if child.is_symlink() or not child.is_dir():
                raise MalformedEvidence(
                    "external stream anchor PR entry must be a real directory: {0}".format(
                        child
                    )
                )
            numbers.append(int(child.name))
        return tuple(numbers)

    def _project_policy_adoption_events_unlocked(self) -> Tuple[dict, ...]:
        project_dir = self._root / self._project
        external_prs = set(self._external_anchor_pr_numbers())
        try:
            entry = project_dir.lstat()
        except FileNotFoundError:
            if external_prs:
                raise MalformedEvidence(
                    "project event store is missing while an external stream anchor exists"
                )
            return ()
        except OSError as error:
            raise MalformedEvidence(
                "cannot validate project adoption root: {0}".format(project_dir)
            ) from error
        if not stat.S_ISDIR(entry.st_mode) or project_dir.is_symlink():
            raise MalformedEvidence(
                "project adoption root must be a real directory: {0}".format(project_dir)
            )
        events = []
        try:
            children = sorted(project_dir.iterdir(), key=lambda child: child.name)
        except OSError as error:
            raise MalformedEvidence(
                "cannot enumerate project adoption root: {0}".format(project_dir)
            ) from error
        local_prs = set()
        for child in children:
            if child.name == ".policy-adoption.lock":
                if child.is_symlink() or not child.is_file():
                    raise MalformedEvidence(
                        "policy adoption lock must be a regular file: {0}".format(child)
                    )
                continue
            if re.fullmatch(r"[1-9][0-9]*", child.name) is None:
                raise MalformedEvidence(
                    "project adoption child must be a positive PR directory: {0}".format(
                        child
                    )
                )
            try:
                child_pr = int(child.name)
            except ValueError as error:
                raise MalformedEvidence(
                    "project adoption PR directory number is invalid: {0}".format(child)
                ) from error
            local_prs.add(child_pr)
            if child.is_symlink() or not child.is_dir():
                raise MalformedEvidence(
                    "project adoption PR entry must be a real directory: {0}".format(
                        child
                    )
                )
        for child_pr in sorted(local_prs | external_prs):
            child_store = RunStore(self._root, self._project, child_pr)
            child_events = child_store._read_authoritative_events()
            activations = {
                event["event_id"]: event
                for event in child_events
                if event["event_type"] == POLICY_ACTIVATION_EVENT
            }
            for event in child_events:
                if event["event_type"] != POLICY_ADOPTION_EVENT:
                    continue
                activation = activations.get(event["activation_event_id"])
                if activation is None:
                    raise MalformedEvidence(
                        "policy adoption event does not bind a local policy activation"
                    )
                if (
                    activation["commit"] != event["commit"]
                    or activation.get("staged_config_digest")
                    != event["staged_config_digest"]
                    or activation.get("projected_enforced_config_digest")
                    != event["enforced_config_digest"]
                ):
                    raise MalformedEvidence(
                        "policy adoption event does not match its activation evidence"
                    )
                events.append(event)
        return tuple(events)

    def current_policy_adoption(self, repository: str) -> Optional[dict]:
        with self._policy_adoption_lock(exclusive=False) as acquired:
            if not acquired:
                return None
            return self._current_policy_adoption_unlocked(repository)

    def _current_policy_adoption_unlocked(self, repository: str) -> Optional[dict]:
        """Return the sole verified audit leaf of the project's adoption chain."""
        if not isinstance(repository, str) or not repository.strip():
            raise MalformedEvidence("repository must be non-blank")
        events = self.project_policy_adoption_events()
        if not events:
            return None
        for event in events:
            if event["repository"] != repository:
                raise MalformedEvidence(
                    "policy adoption repository does not match the requested repository"
                )
        by_id = {}
        for event in events:
            event_id = event["event_id"]
            if event_id in by_id:
                raise MalformedEvidence("policy adoption transition identity is duplicated")
            by_id[event_id] = event
        roots = [event for event in events if event["previous_transition_id"] is None]
        if len(roots) != 1:
            raise MalformedEvidence("policy adoption chain must have exactly one root")
        referenced = set()
        for event in events:
            previous_id = event["previous_transition_id"]
            if previous_id is None:
                continue
            previous = by_id.get(previous_id)
            if previous is None:
                raise MalformedEvidence("policy adoption predecessor is missing")
            if previous_id in referenced:
                raise MalformedEvidence("policy adoption chain branches")
            referenced.add(previous_id)
            if any(
                event[key] != previous[previous_key]
                for key, previous_key in (
                    ("previous_policy_id", "policy_id"),
                    ("previous_policy_digest", "policy_digest"),
                    ("previous_enforced_config_digest", "enforced_config_digest"),
                )
            ):
                raise MalformedEvidence(
                    "policy adoption predecessor identity does not match"
                )
        leaves = [event for event in events if event["event_id"] not in referenced]
        if len(leaves) != 1:
            raise MalformedEvidence("policy adoption chain must have exactly one leaf")
        head = leaves[0]
        seen = set()
        while head["previous_transition_id"] is not None:
            if head["event_id"] in seen:
                raise MalformedEvidence("policy adoption chain contains a cycle")
            seen.add(head["event_id"])
            head = by_id[head["previous_transition_id"]]
        return leaves[0]

    def record_policy_adoption(
        self,
        repository: str,
        commit,
        *,
        policy_id: str,
        policy_digest: str,
        staged_config_digest: str,
        enforced_config_digest: str,
        activation_event_id: str,
        transition_kind: str,
        previous_adoption: Optional[Mapping] = None,
    ) -> dict:
        with self._policy_adoption_lock():
            return self._record_policy_adoption_locked(
                repository,
                commit,
                policy_id=policy_id,
                policy_digest=policy_digest,
                staged_config_digest=staged_config_digest,
                enforced_config_digest=enforced_config_digest,
                activation_event_id=activation_event_id,
                transition_kind=transition_kind,
                previous_adoption=previous_adoption,
            )

    def _record_policy_adoption_locked(
        self,
        repository: str,
        commit,
        *,
        policy_id: str,
        policy_digest: str,
        staged_config_digest: str,
        enforced_config_digest: str,
        activation_event_id: str,
        transition_kind: str,
        previous_adoption: Optional[Mapping] = None,
    ) -> dict:
        """Append one project-scoped, activation-backed policy transition."""
        commit = _require_commit(commit)
        if not isinstance(repository, str) or not repository.strip():
            raise MalformedEvidence("repository must be non-blank")
        _require_component(policy_id, "policy_id")
        _require_digest(policy_digest)
        _require_digest(staged_config_digest)
        _require_digest(enforced_config_digest)
        _require_digest(activation_event_id)
        if transition_kind not in (
            "initial-adoption",
            "continued-adoption",
            "authorized-rollback",
        ):
            raise MalformedEvidence("transition_kind is not an authorized adoption transition")
        activation = self.matching_policy_activation(
            commit, staged_config_digest, enforced_config_digest
        )
        if activation is None or activation.get("event_id") != activation_event_id:
            raise EvidenceConflict("policy adoption lacks matching activation evidence")
        actual_previous = self._current_policy_adoption_unlocked(repository)
        actual_previous_id = (
            actual_previous.get("event_id") if actual_previous is not None else None
        )
        supplied_previous_id = (
            previous_adoption.get("event_id")
            if previous_adoption is not None
            else None
        )
        if actual_previous_id != supplied_previous_id:
            raise EvidenceConflict("policy adoption predecessor changed concurrently")
        if transition_kind == "initial-adoption":
            if actual_previous is not None:
                raise EvidenceConflict("initial adoption cannot replace an existing policy")
            previous_fields = {
                "previous_transition_id": None,
                "previous_policy_id": None,
                "previous_policy_digest": None,
                "previous_enforced_config_digest": None,
            }
        else:
            if actual_previous is None:
                raise EvidenceConflict("non-initial adoption requires an existing policy")
            previous_fields = {
                "previous_transition_id": actual_previous["event_id"],
                "previous_policy_id": actual_previous["policy_id"],
                "previous_policy_digest": actual_previous["policy_digest"],
                "previous_enforced_config_digest": actual_previous[
                    "enforced_config_digest"
                ],
            }
        identity = {
            "transition_kind": transition_kind,
            "activation_event_id": activation_event_id,
            "repository": repository,
            "policy_id": policy_id,
            "policy_digest": policy_digest,
            "mode": "enforced",
            "staged_config_digest": staged_config_digest,
            "enforced_config_digest": enforced_config_digest,
            **previous_fields,
        }
        event_id = self._policy_adoption_event_id(
            {
                **identity,
                "project": self._project,
                "pr": self._pr,
                "commit": commit,
            }
        )
        for event in self.project_policy_adoption_events():
            if event["event_id"] == event_id:
                if all(event.get(key) == value for key, value in identity.items()):
                    return event
                raise EvidenceConflict("policy adoption identity collides with existing evidence")
        return self._append_event(
            POLICY_ADOPTION_EVENT,
            commit,
            os.fspath(self.events_path),
            payload={"event_id": event_id, **identity},
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

    def _stream_anchor_identity(self) -> Optional[dict]:
        """Read the immutable external identity, without creating migration state."""
        directory = self.stream_anchor_dir
        try:
            entry = directory.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise MalformedEvidence(f"cannot validate stream anchor: {directory}") from error
        if not stat.S_ISDIR(entry.st_mode) or directory.is_symlink():
            raise MalformedEvidence(f"stream anchor must be a real directory: {directory}")
        try:
            children = {child.name for child in directory.iterdir()}
        except OSError as error:
            raise MalformedEvidence(f"cannot enumerate stream anchor: {directory}") from error
        allowed = {"identity.json", "head.jsonl"}
        unknown = children - allowed
        if unknown:
            raise MalformedEvidence(
                "stream anchor contains unknown entry(s): {0}".format(sorted(unknown))
            )
        if "identity.json" not in children:
            if children:
                raise MalformedEvidence("stream anchor identity is missing")
            return None
        try:
            raw, _digest = read_contained_file(
                self.stream_anchor_identity_path, self._root, "stream anchor identity"
            )
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence(
                f"cannot read stream anchor identity: {self.stream_anchor_identity_path}"
            ) from error
        identity = _parse_json_bytes(
            raw, self.stream_anchor_identity_path, "stream anchor identity"
        )
        if frozenset(identity) != _STREAM_ANCHOR_IDENTITY_KEYS:
            raise MalformedEvidence("stream anchor identity keys are not exact")
        if identity.get("schema_version") != 1:
            raise MalformedEvidence("unsupported stream anchor identity schema")
        if identity.get("project") != self._project or identity.get("pr") != self._pr:
            raise MalformedEvidence("stream anchor identity does not bind project and PR")
        if _DIGEST_RE.fullmatch(identity.get("stream_id", "")) is None:
            raise MalformedEvidence("stream anchor identity has an invalid stream_id")
        if identity.get("events_path") != _resolve(os.fspath(self.events_path)):
            raise MalformedEvidence("stream anchor identity does not bind events path")
        for key in ("events_device", "events_inode", "initial_size", "initial_event_count"):
            value = identity.get(key)
            if type(value) is not int or value < 0:
                raise MalformedEvidence("stream anchor identity has invalid {0}".format(key))
        if _DIGEST_RE.fullmatch(identity.get("initial_sha256", "")) is None:
            raise MalformedEvidence("stream anchor identity has an invalid initial digest")
        if identity["initial_size"] == 0 and identity["initial_sha256"] != _EMPTY_STREAM_DIGEST:
            raise MalformedEvidence("empty stream anchor has a non-empty initial digest")
        identity_entry = self.stream_anchor_identity_path.lstat()
        if not stat.S_ISREG(identity_entry.st_mode) or identity_entry.st_nlink != 1:
            raise MalformedEvidence("stream anchor identity must be a single-link regular file")
        return {
            "identity": identity,
            "anchor_identity": (entry.st_dev, entry.st_ino),
            "identity_identity": (identity_entry.st_dev, identity_entry.st_ino),
        }

    @staticmethod
    def _anchor_operation_id(record: Mapping) -> str:
        value = dict(record)
        value.pop("operation_id", None)
        value.pop("phase", None)
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _parse_anchor_head(
        self, raw: bytes, path: Path, anchor: dict, events_identity, head_identity
    ):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MalformedEvidence(f"stream anchor head is not UTF-8: {path}") from error
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        state = {
            "size": anchor["identity"]["initial_size"],
            "sha256": anchor["identity"]["initial_sha256"],
            "event_count": anchor["identity"]["initial_event_count"],
        }
        pending = None
        seen_operations = set()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                raise MalformedEvidence(f"blank line in stream anchor head at line {number}")
            try:
                record = strict_json_loads(line, "stream anchor head record")
            except StrictJsonError as error:
                raise MalformedEvidence(
                    f"malformed stream anchor head at line {number}: {error}"
                ) from error
            record = _require_json_object(record, "stream anchor head record")
            if frozenset(record) != _STREAM_ANCHOR_HEAD_KEYS:
                raise MalformedEvidence("stream anchor head record keys are not exact")
            if record.get("schema_version") != 1 or record.get("record_type") != "event-update":
                raise MalformedEvidence("unsupported stream anchor head record")
            if record.get("project") != self._project or record.get("pr") != self._pr:
                raise MalformedEvidence("stream anchor head record does not bind project and PR")
            if record.get("stream_id") != anchor["identity"]["stream_id"]:
                raise MalformedEvidence("stream anchor head stream identity disagrees")
            if record.get("phase") not in _STREAM_ANCHOR_PHASES:
                raise MalformedEvidence("stream anchor head phase is invalid")
            operation_id = record.get("operation_id")
            if _DIGEST_RE.fullmatch(operation_id or "") is None:
                raise MalformedEvidence("stream anchor head operation_id is invalid")
            if self._anchor_operation_id(record) != operation_id:
                raise MalformedEvidence("stream anchor head operation is not envelope-bound")
            if (
                (record["anchor_device"], record["anchor_inode"]) != anchor["anchor_identity"]
                or (record["identity_device"], record["identity_inode"]) != anchor["identity_identity"]
                or (record["head_device"], record["head_inode"]) != head_identity
                or (record["events_device"], record["events_inode"]) != events_identity
            ):
                raise MalformedEvidence("stream anchor head identity disagrees with its files")
            for key in (
                "anchor_device", "anchor_inode", "identity_device", "identity_inode",
                "head_device", "head_inode",
                "events_device", "events_inode", "previous_size", "previous_event_count",
                "new_size", "new_event_count", "line_size",
            ):
                value = record.get(key)
                if type(value) is not int or value < 0:
                    raise MalformedEvidence("stream anchor head has invalid {0}".format(key))
            for key in ("previous_sha256", "new_sha256", "line_sha256"):
                if _DIGEST_RE.fullmatch(record.get(key, "")) is None:
                    raise MalformedEvidence("stream anchor head has invalid {0}".format(key))
            if (
                record["previous_size"] != state["size"]
                or record["previous_sha256"] != state["sha256"]
                or record["previous_event_count"] != state["event_count"]
            ):
                raise MalformedEvidence("stream anchor head previous state disagrees")
            if record["new_size"] != record["previous_size"] + record["line_size"]:
                raise MalformedEvidence("stream anchor head size transition is invalid")
            if record["new_event_count"] != record["previous_event_count"] + 1:
                raise MalformedEvidence("stream anchor head event count transition is invalid")
            if record["phase"] == "PREPARE":
                if pending is not None or operation_id in seen_operations:
                    raise MalformedEvidence("stream anchor head has consecutive prepares")
                seen_operations.add(operation_id)
                pending = record
            else:
                if pending is None or record["operation_id"] != pending["operation_id"]:
                    raise MalformedEvidence("stream anchor head completion has no matching prepare")
                for key in _STREAM_ANCHOR_HEAD_KEYS - {"phase", "operation_id"}:
                    if record[key] != pending[key]:
                        raise MalformedEvidence("stream anchor head completion disagrees with prepare")
                if record["phase"] == "COMMIT":
                    state = {
                        "size": record["new_size"],
                        "sha256": record["new_sha256"],
                        "event_count": record["new_event_count"],
                    }
                pending = None
        return state, pending

    def _read_stream_snapshot(self, anchor: dict):
        try:
            raw, digest = read_contained_file(self.events_path, self._root, "events")
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence(f"cannot read anchored events: {self.events_path}") from error
        entry = self.events_path.lstat()
        events_identity = (entry.st_dev, entry.st_ino)
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise MalformedEvidence("anchored events must be a single-link regular file")
        identity = anchor["identity"]
        if events_identity != (identity["events_device"], identity["events_inode"]):
            raise MalformedEvidence("events file identity disagrees with external anchor")
        return raw, digest, events_identity

    def _read_anchor_state(self, anchor: dict):
        raw, digest, events_identity = self._read_stream_snapshot(anchor)
        head_path = self.stream_anchor_head_path
        head_identity = None
        try:
            head_raw, _head_digest = read_contained_file(head_path, self._root, "stream anchor head")
            head_entry = head_path.lstat()
            if not stat.S_ISREG(head_entry.st_mode) or head_entry.st_nlink != 1:
                raise MalformedEvidence("stream anchor head must be a single-link regular file")
            head_identity = (head_entry.st_dev, head_entry.st_ino)
            state, pending = self._parse_anchor_head(
                head_raw, head_path, anchor, events_identity, head_identity
            )
        except FileNotFoundError:
            state, pending = (
                {
                    "size": anchor["identity"]["initial_size"],
                    "sha256": anchor["identity"]["initial_sha256"],
                    "event_count": anchor["identity"]["initial_event_count"],
                },
                None,
            )
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence("cannot read stream anchor head") from error
        return raw, digest, events_identity, head_identity, state, pending

    def _anchored_events_bytes(self, anchor: dict):
        raw, digest, _events_identity, _head_identity, state, pending = self._read_anchor_state(anchor)
        if pending is not None:
            raise MalformedEvidence("stream anchor update is incomplete; replay is required")
        if (len(raw), digest) != (state["size"], state["sha256"]):
            raise MalformedEvidence("events head does not match the external stream anchor")
        return raw, state["event_count"]

    def _parse_events_bytes(self, raw: bytes, path: Path) -> Tuple[dict, ...]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MalformedEvidence(f"events are not valid UTF-8: {path}: {error}") from error
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        events = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                raise MalformedEvidence(f"blank line in events at line {number}: {path}")
            try:
                parsed = strict_json_loads(line, f"event at line {number}")
            except StrictJsonError as error:
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
            if event["event_type"] == POLICY_ADOPTION_EVENT:
                self._require_policy_adoption_event(event, number, path)
            events.append(event)
        return tuple(events)

    def read_events(self) -> Tuple[dict, ...]:
        """Read events, retaining an unanchored historical parser for migration diagnostics."""
        with self._policy_adoption_lock(exclusive=False) as acquired:
            if not acquired:
                return ()
            return self._read_events_unlocked(require_anchor=False)

    def _read_authoritative_events(self) -> Tuple[dict, ...]:
        with self._policy_adoption_lock(exclusive=False) as acquired:
            if not acquired:
                return ()
            return self._read_events_unlocked(require_anchor=True)

    def _read_events_unlocked(self, *, require_anchor=False) -> Tuple[dict, ...]:
        anchor = self._stream_anchor_identity()
        if anchor is None:
            try:
                raw, _digest = read_contained_file(self.events_path, self._root, "events")
            except FileNotFoundError:
                if require_anchor and self._base.exists():
                    try:
                        if any(
                            child.name not in _EVENT_STORE_OPERATIONAL_ENTRIES
                            for child in self._base.iterdir()
                        ):
                            raise MalformedEvidence(
                                "unanchored event stream requires explicit migration"
                            )
                    except OSError as error:
                        raise MalformedEvidence("cannot inspect unanchored event store") from error
                return ()
            except (OSError, SecurePathError) as error:
                raise MalformedEvidence(f"cannot read events: {self.events_path}: {error}") from error
            if not raw:
                return ()
            if require_anchor:
                raise MalformedEvidence("unanchored event stream requires explicit migration")
            return self._parse_events_bytes(raw, self.events_path)
        raw, event_count = self._anchored_events_bytes(anchor)
        events = self._parse_events_bytes(raw, self.events_path)
        if len(events) != event_count:
            raise MalformedEvidence("external stream anchor event count disagrees")
        return events

    def current_commit(self) -> Optional[str]:
        """Most recent recorded pushed commit, or None before the first commit event."""
        current = None
        for event in self._read_authoritative_events():
            if event["event_type"] == COMMIT_EVENT:
                current = event["commit"]
        return current

    def _append_line(self, line: bytes) -> None:
        with self._policy_adoption_lock():
            anchor = self._stream_anchor_identity()
            if anchor is None:
                self._initialize_stream_anchor_locked()
                anchor = self._stream_anchor_identity()
            if anchor is None:
                raise MalformedEvidence("stream anchor initialization did not persist")
            if not line:
                self._read_anchor_state(anchor)
                return
            raw, digest, events_identity, head_identity, state, pending = self._read_anchor_state(anchor)
            if pending is not None:
                self._replay_pending_anchor_locked(anchor, pending, events_identity, state, head_identity)
                anchor = self._stream_anchor_identity()
                raw, digest, events_identity, head_identity, state, pending = self._read_anchor_state(anchor)
                if pending is not None:
                    raise MalformedEvidence("stream anchor replay did not complete")
            if (len(raw), digest) != (state["size"], state["sha256"]):
                raise MalformedEvidence("events head does not match the external stream anchor")
            if head_identity is None:
                try:
                    secure_append_contained_file(
                        self.stream_anchor_head_path,
                        self._root,
                        b"",
                        "stream anchor head",
                        create=True,
                    )
                except (OSError, SecurePathError) as error:
                    raise MalformedEvidence("cannot initialize stream anchor head") from error
                head_entry = self.stream_anchor_head_path.lstat()
                head_identity = (head_entry.st_dev, head_entry.st_ino)
            new_raw = raw + line
            new_digest = hashlib.sha256(new_raw).hexdigest()
            operation = self._anchor_update_record(
                anchor,
                events_identity,
                head_identity,
                state,
                {
                    "size": len(new_raw),
                    "sha256": new_digest,
                    "event_count": state["event_count"] + 1,
                },
                line,
                "PREPARE",
            )
            self._append_anchor_record(operation, head_identity, create=head_identity is None)
            try:
                secure_append_contained_file(
                    self.events_path,
                    self._root,
                    line,
                    "events",
                    create=False,
                    expected_identity=events_identity,
                )
            except PermissionError:
                self._replay_pending_anchor_locked(
                    anchor, operation, events_identity, state, head_identity
                )
                raise
            except (OSError, SecurePathError) as error:
                self._replay_pending_anchor_locked(
                    anchor, operation, events_identity, state, head_identity
                )
                raise MalformedEvidence(
                    f"cannot append events: {self.events_path}: {error}"
                ) from error
            current_raw, current_digest, current_identity = self._read_stream_snapshot(anchor)
            if (
                current_identity != events_identity
                or (len(current_raw), current_digest)
                != (operation["new_size"], operation["new_sha256"])
            ):
                raise MalformedEvidence("events changed before stream anchor commit")
            completion = dict(operation)
            completion["phase"] = "COMMIT"
            completion["operation_id"] = self._anchor_operation_id(completion)
            if completion["operation_id"] != operation["operation_id"]:
                raise MalformedEvidence("stream anchor completion identity changed")
            self._append_anchor_record(completion, head_identity, create=False)

    def _initialize_stream_anchor_locked(self, initial_raw: bytes = b"") -> None:
        """Create the external identity for a new or explicitly migrated stream."""
        try:
            current_raw, _digest = read_contained_file(
                self.events_path, self._root, "events"
            )
        except FileNotFoundError:
            if initial_raw:
                raise MalformedEvidence("cannot migrate a missing event stream")
            try:
                secure_append_contained_file(
                    self.events_path, self._root, b"", "events", create=True
                )
            except (OSError, SecurePathError) as error:
                raise MalformedEvidence("cannot initialize event stream") from error
            current_raw = b""
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence("cannot inspect event stream for anchoring") from error
        if current_raw != initial_raw:
            raise MalformedEvidence("event stream changed before anchoring")
        entry = self.events_path.lstat()
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise MalformedEvidence("events must be a single-link regular file")
        ensure_directory(self.stream_anchor_dir)
        identity = {
            "schema_version": 1,
            "project": self._project,
            "pr": self._pr,
            "stream_id": secrets.token_hex(32),
            "events_path": _resolve(os.fspath(self.events_path)),
            "events_device": entry.st_dev,
            "events_inode": entry.st_ino,
            "initial_size": len(current_raw),
            "initial_sha256": hashlib.sha256(current_raw).hexdigest(),
            "initial_event_count": len(self._parse_events_bytes(current_raw, self.events_path))
            if current_raw
            else 0,
        }
        raw = _serialize(identity)
        try:
            created = secure_atomic_create(self.stream_anchor_identity_path, raw)
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence("cannot persist stream anchor identity") from error
        if not created:
            existing = self._stream_anchor_identity()
            if existing is None or existing["identity"] != identity:
                raise EvidenceConflict("stream anchor identity already exists with different bytes")
        try:
            secure_atomic_create(self.stream_anchor_head_path, b"")
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence("cannot initialize stream anchor head") from error

    def _anchor_update_record(
        self, anchor, events_identity, head_identity, previous, new, line, phase
    ) -> dict:
        if head_identity is None:
            raise MalformedEvidence("stream anchor head identity is missing before update")
        anchor_device, anchor_inode = anchor["anchor_identity"]
        identity_device, identity_inode = anchor["identity_identity"]
        head_device, head_inode = head_identity
        record = {
            "schema_version": 1,
            "record_type": "event-update",
            "phase": phase,
            "project": self._project,
            "pr": self._pr,
            "stream_id": anchor["identity"]["stream_id"],
            "anchor_device": anchor_device,
            "anchor_inode": anchor_inode,
            "identity_device": identity_device,
            "identity_inode": identity_inode,
            "head_device": head_device,
            "head_inode": head_inode,
            "events_device": events_identity[0],
            "events_inode": events_identity[1],
            "previous_size": previous["size"],
            "previous_sha256": previous["sha256"],
            "previous_event_count": previous["event_count"],
            "new_size": new["size"],
            "new_sha256": new["sha256"],
            "new_event_count": new["event_count"],
            "line_size": len(line),
            "line_sha256": hashlib.sha256(line).hexdigest(),
        }
        record["operation_id"] = self._anchor_operation_id(record)
        return record

    def _append_anchor_record(self, record: dict, expected_identity, *, create):
        raw = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        try:
            secure_append_contained_file(
                self.stream_anchor_head_path,
                self._root,
                raw,
                "stream anchor head",
                create=create,
                expected_identity=expected_identity,
            )
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence("cannot update stream anchor head") from error

    def _replay_pending_anchor_locked(
        self, anchor, pending, events_identity, state, head_identity
    ) -> None:
        raw, digest, current_identity = self._read_stream_snapshot(anchor)
        previous = {
            "size": pending["previous_size"],
            "sha256": pending["previous_sha256"],
            "event_count": pending["previous_event_count"],
        }
        new = {
            "size": pending["new_size"],
            "sha256": pending["new_sha256"],
            "event_count": pending["new_event_count"],
        }
        if current_identity != events_identity:
            raise MalformedEvidence("pending stream update has a different event inode")
        if (len(raw), digest) == (new["size"], new["sha256"]):
            completion = dict(pending)
            completion["phase"] = "COMMIT"
        elif (len(raw), digest) == (previous["size"], previous["sha256"]):
            completion = dict(pending)
            completion["phase"] = "ABORT"
        else:
            raise MalformedEvidence("pending stream update has an unrecognized event head")
        completion["operation_id"] = self._anchor_operation_id(completion)
        if completion["operation_id"] != pending["operation_id"]:
            raise MalformedEvidence("pending stream update identity changed")
        self._append_anchor_record(completion, head_identity, create=False)

    def recover_event_stream(self) -> None:
        """Replay a crash-pending anchor update without accepting a truncation."""
        with self._policy_adoption_lock():
            anchor = self._stream_anchor_identity()
            if anchor is None:
                return
            _raw, _digest, events_identity, head_identity, _state, pending = self._read_anchor_state(anchor)
            if pending is not None:
                self._replay_pending_anchor_locked(
                    anchor, pending, events_identity, _state, head_identity
                )
            self._anchored_events_bytes(anchor)

    def migrate_event_stream(self) -> bool:
        """Explicitly bind one valid legacy stream; never upgrades policy authority."""
        with self._policy_adoption_lock():
            anchor = self._stream_anchor_identity()
            if anchor is not None:
                self._anchored_events_bytes(anchor)
                return False
            try:
                raw, _digest = read_contained_file(self.events_path, self._root, "legacy events")
            except FileNotFoundError:
                return False
            except (OSError, SecurePathError) as error:
                raise MalformedEvidence("cannot read legacy event stream for migration") from error
            if not raw:
                return False
            events = self._parse_events_bytes(raw, self.events_path)
            if len(events) == 0:
                return False
            self._initialize_stream_anchor_locked(raw)
            anchor = self._stream_anchor_identity()
            if anchor is None:
                raise MalformedEvidence("legacy event stream migration did not persist its anchor")
            self._anchored_events_bytes(anchor)
            return True

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
        return self.write_review_bytes(commit, review_id, line)

    def write_review_bytes(self, commit, review_id, line: bytes) -> Path:
        """Persist already-validated review bytes without reserialization."""
        commit = _require_commit(commit)
        review_id = _require_review_id(review_id)
        if not isinstance(line, bytes):
            raise MalformedEvidence("review record bytes must be bytes")
        record = _parse_json_bytes(line, self.review_path(commit, review_id), "review record")
        if "reviewed_commit" in record and record["reviewed_commit"] != commit:
            raise MalformedEvidence("review record reviewed_commit contradicts its store path")
        target = self.review_path(commit, review_id)
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

    # -- durable review retirement ----------------------------------------

    def retirement_dir(self, commit, retirement_id) -> Path:
        return (
            self._base
            / "retirements"
            / _require_commit(commit)
            / _require_component(retirement_id, "retirement_id")
        )

    def retirement_envelope_path(self, commit, retirement_id) -> Path:
        return self.retirement_dir(commit, retirement_id) / "envelope.json"

    def retirement_staging_path(self, commit, retirement_id) -> Path:
        return (
            self._base
            / "reviews-retirement-staging"
            / _require_commit(commit)
            / ("{0}.json".format(_require_component(retirement_id, "retirement_id")))
        )

    def retirement_final_path(self, commit, retirement_id) -> Path:
        return (
            self._base
            / "reviews-retired"
            / _require_commit(commit)
            / ("{0}.json".format(_require_component(retirement_id, "retirement_id")))
        )

    def _retirement_event_payload(self, envelope, event_type) -> dict:
        suffix = {
            RETIREMENT_PREPARED_EVENT: "prepared",
            RETIREMENT_COPIED_EVENT: "copied",
            RETIREMENT_COMMITTED_EVENT: "committed",
            RETIREMENT_FINALIZED_EVENT: "finalized",
        }[event_type]
        return {
            "event_id": "{0}:{1}".format(envelope["retirement_id"], suffix),
            "operation": envelope["operation"],
            "retirement_id": envelope["retirement_id"],
            "repository": envelope["repository"],
            "review_id": envelope["review_id"],
            "source_path": envelope["source_path"],
            "staging_path": _resolve(
                os.fspath(self.retirement_staging_path(envelope["commit"], envelope["retirement_id"]))
            ),
            "final_path": _resolve(
                os.fspath(self.retirement_final_path(envelope["commit"], envelope["retirement_id"]))
            ),
            "expected_sha256": envelope["expected_sha256"],
            "reason": envelope["reason"],
            "policy_id": envelope["policy_id"],
        }

    def _require_retirement_envelope(
        self, envelope, commit, retirement_id, *, bind_to_store=True
    ) -> dict:
        if not isinstance(envelope, dict):
            raise MalformedEvidence("retirement envelope must be a JSON object")
        if frozenset(envelope) != _RETIREMENT_ENVELOPE_KEYS:
            raise MalformedEvidence("retirement envelope keys are not exact")
        if envelope["schema_version"] != 1 or isinstance(envelope["schema_version"], bool):
            raise MalformedEvidence("retirement envelope schema_version must be 1")
        if envelope["operation"] != "REVIEW_RETIREMENT":
            raise MalformedEvidence("retirement envelope operation is invalid")
        try:
            envelope_project = _require_component(envelope["project"], "project")
            envelope_pr = _require_pr(envelope["pr_number"])
            envelope_commit = _require_commit(envelope["commit"])
            envelope_retirement_id = _require_component(
                envelope["retirement_id"], "retirement_id"
            )
        except StoreError as error:
            raise MalformedEvidence("retirement envelope identity is malformed") from error
        if bind_to_store and (
            envelope_project != self._project
            or envelope_pr != self._pr
            or envelope_commit != _require_commit(commit)
            or envelope_retirement_id != _require_component(retirement_id, "retirement_id")
        ):
            raise MalformedEvidence("retirement envelope does not bind to this store")
        _require_review_id(envelope["review_id"])
        if not isinstance(envelope["repository"], str) or not envelope["repository"].strip():
            raise MalformedEvidence("retirement envelope repository must be non-blank")
        source = _resolve(
            os.fspath(
                self._root
                / envelope_project
                / str(envelope_pr)
                / "reviews"
                / envelope_commit
                / (envelope["review_id"] + ".json")
            )
        )
        if envelope["source_path"] != source:
            raise MalformedEvidence("retirement envelope source path is not canonical")
        _require_digest(envelope["expected_sha256"])
        if envelope["reason"] not in RETIREMENT_REASONS:
            raise MalformedEvidence("retirement reason is not an approved policy reason")
        _require_component(envelope["policy_id"], "policy_id")
        requested_at = envelope["requested_at"]
        if not isinstance(requested_at, str):
            raise MalformedEvidence("retirement requested_at must be a UTC timestamp")
        try:
            parsed = datetime.fromisoformat(requested_at)
        except ValueError as error:
            raise MalformedEvidence("retirement requested_at is not ISO-8601") from error
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise MalformedEvidence("retirement requested_at must be timezone-aware UTC")
        return envelope

    def _read_retirement_envelope(self, commit, retirement_id) -> Optional[dict]:
        path = self.retirement_envelope_path(commit, retirement_id)
        try:
            raw, _digest = self._read_bound_bytes(path, "retirement envelope")
        except FileNotFoundError:
            return None
        except MalformedEvidence as error:
            if not path.exists() and not path.is_symlink():
                return None
            raise
        except OSError as error:
            raise MalformedEvidence("cannot read retirement envelope: {0}".format(path)) from error
        return self._require_retirement_envelope(
            _parse_json_bytes(raw, path, "retirement envelope"),
            commit,
            retirement_id,
            bind_to_store=True,
        )

    def _retirement_events(self, commit, retirement_id, events=None):
        commit = _require_commit(commit)
        retirement_id = _require_component(retirement_id, "retirement_id")
        if events is None:
            events = self._read_authoritative_events()
        selected = [
            event
            for event in events
            if event["commit"] == commit
            and event.get("event_type") in RETIREMENT_EVENT_TYPES
            and event.get("retirement_id") == retirement_id
        ]
        ids = [event.get("event_id") for event in selected]
        if any(not isinstance(value, str) or not value.strip() for value in ids):
            raise MalformedEvidence("retirement events require non-blank event_id values")
        if len(set(ids)) != len(ids):
            raise MalformedEvidence("retirement event IDs must be unique")
        return selected

    def _validate_retirement_event(self, event, envelope, event_type):
        if event["event_type"] != event_type:
            raise MalformedEvidence("retirement event is out of order")
        missing = sorted(_RETIREMENT_EVENT_KEYS - set(event))
        if missing:
            raise MalformedEvidence("retirement event is missing: {0}".format(", ".join(missing)))
        expected = self._retirement_event_payload(envelope, event_type)
        if any(event.get(key) != value for key, value in expected.items()):
            raise MalformedEvidence("retirement event does not match its immutable envelope")

    def _retirement_state(self, commit, retirement_id, events=None):
        envelope = self._read_retirement_envelope(commit, retirement_id)
        if envelope is None:
            return None, None
        selected = self._retirement_events(commit, retirement_id, events)
        if not selected or selected[0]["event_type"] != RETIREMENT_PREPARED_EVENT:
            raise MalformedEvidence("retirement envelope is not bound by PREPARED")
        expected_types = [event["event_type"] for event in selected]
        if expected_types != list(dict.fromkeys(expected_types)):
            raise MalformedEvidence("retirement events contain duplicate transitions")
        allowed_prefixes = [
            [RETIREMENT_PREPARED_EVENT],
            [RETIREMENT_PREPARED_EVENT, RETIREMENT_COPIED_EVENT],
            [RETIREMENT_PREPARED_EVENT, RETIREMENT_COPIED_EVENT, RETIREMENT_COMMITTED_EVENT],
            list(RETIREMENT_EVENT_TYPES),
        ]
        if expected_types not in allowed_prefixes:
            raise MalformedEvidence("retirement events are not a valid state-machine prefix")
        for event_type, event in zip(expected_types, selected):
            self._validate_retirement_event(event, envelope, event_type)

        source = Path(envelope["source_path"])
        staging = self.retirement_staging_path(commit, retirement_id)
        final = self.retirement_final_path(commit, retirement_id)
        source_exists = source.exists()
        staging_exists = staging.exists()
        final_exists = final.exists()
        if source_exists:
            _source_raw, source_digest = self._read_bound_bytes(source, "active review record")
            if source_digest != envelope["expected_sha256"]:
                raise MalformedEvidence("active review digest changed during retirement")
        if RETIREMENT_COPIED_EVENT in expected_types:
            if not staging_exists:
                raise MalformedEvidence("COPIED retirement is missing its staging proof")
            _staging_raw, staging_digest = self._read_bound_bytes(staging, "retirement staging proof")
            if staging_digest != envelope["expected_sha256"]:
                raise MalformedEvidence("retirement staging digest does not match")
        elif staging_exists:
            raise MalformedEvidence("orphan retirement staging proof")
        if RETIREMENT_COMMITTED_EVENT not in expected_types and final_exists:
            raise MalformedEvidence("retirement final artifact exists before COMMITTED")
        if final_exists:
            _final_raw, final_digest = self._read_bound_bytes(final, "retired review record")
            if final_digest != envelope["expected_sha256"]:
                raise MalformedEvidence("retired review digest does not match")
        if RETIREMENT_FINALIZED_EVENT in expected_types:
            if source_exists or not staging_exists or not final_exists:
                raise MalformedEvidence("FINALIZED retirement does not have the required paths")
            state = "FINALIZED"
        elif RETIREMENT_COMMITTED_EVENT in expected_types:
            state = "COMMITTED"
        elif RETIREMENT_COPIED_EVENT in expected_types:
            state = "COPIED"
        else:
            state = "PREPARED"
        return state, envelope

    def retirement_status(self, commit, retirement_id) -> dict:
        with self._policy_adoption_lock(exclusive=False) as acquired:
            if not acquired:
                return {
                    "state": "ACTIVE",
                    "retirement_id": _require_component(retirement_id, "retirement_id"),
                }
            state, envelope = self._retirement_state(commit, retirement_id)
            if envelope is None:
                return {
                    "state": "ACTIVE",
                    "retirement_id": _require_component(retirement_id, "retirement_id"),
                }
            return {"state": state, **envelope}

    def _append_retirement_transition(self, event_type, envelope, commit, events):
        event_id = self._retirement_event_payload(envelope, event_type)["event_id"]
        if any(event.get("event_id") == event_id for event in events):
            return
        self.append_event(
            event_type,
            commit,
            os.fspath(self.retirement_envelope_path(commit, envelope["retirement_id"])),
            payload=self._retirement_event_payload(envelope, event_type),
        )

    def _move_no_replace(self, source: Path, target: Path, expected_digest: str) -> None:
        if target.exists() or target.is_symlink():
            _target_raw, target_digest = self._read_bound_bytes(target, "retired review record")
            if target_digest != expected_digest:
                raise EvidenceConflict("retirement final path already contains different bytes")
            if source.exists():
                raise MalformedEvidence("retirement final and active source both exist")
            return
        try:
            ensure_directory(target.parent)
            rename_no_replace(source, target)
        except FileExistsError:
            self._move_no_replace(source, target, expected_digest)
            return
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence("platform cannot provide no-replace retirement move") from error

    def retire_review(
        self,
        repository,
        commit,
        review_id,
        retirement_id,
        reason,
        policy_id,
        expected_sha256,
    ) -> dict:
        with self._policy_adoption_lock():
            return self._retire_review_locked(
                repository,
                commit,
                review_id,
                retirement_id,
                reason,
                policy_id,
                expected_sha256,
            )

    def _retire_review_locked(
        self,
        repository,
        commit,
        review_id,
        retirement_id,
        reason,
        policy_id,
        expected_sha256,
    ) -> dict:
        commit = _require_commit(commit)
        review_id = _require_review_id(review_id)
        retirement_id = _require_component(retirement_id, "retirement_id")
        if not isinstance(repository, str) or not repository.strip():
            raise MalformedEvidence("repository must be non-blank")
        if reason not in RETIREMENT_REASONS:
            raise MalformedEvidence("retirement reason is not an approved policy reason")
        _require_component(policy_id, "policy_id")
        expected_sha256 = _require_digest(expected_sha256)
        source = self.review_path(commit, review_id)
        envelope_path = self.retirement_envelope_path(commit, retirement_id)
        envelope = self._read_retirement_envelope(commit, retirement_id)
        if envelope is not None:
            if any(
                envelope.get(key) != value
                for key, value in (
                    ("repository", repository),
                    ("review_id", review_id),
                    ("source_path", _resolve(os.fspath(source))),
                    ("expected_sha256", expected_sha256),
                    ("reason", reason),
                    ("policy_id", policy_id),
                )
            ):
                raise EvidenceConflict("retirement id already has different immutable arguments")
            return self.recover_retirement(retirement_id, commit)
        if not source.exists():
            raise MalformedEvidence("retirement source review does not exist")
        raw, actual_digest = self._read_bound_bytes(source, "active review record")
        if actual_digest != expected_sha256:
            raise EvidenceConflict("retirement source digest does not match expected_sha256")
        self.validate_event_relations(commit)
        events = self._read_authoritative_events()
        staging_path = self.retirement_staging_path(commit, retirement_id)
        final_path = self.retirement_final_path(commit, retirement_id)
        if staging_path.exists() or staging_path.is_symlink():
            raise EvidenceConflict(
                "pre-existing retirement staging lacks its matching envelope and event sequence"
            )
        if final_path.exists() or final_path.is_symlink():
            raise EvidenceConflict(
                "pre-existing retirement final lacks its matching envelope and event sequence"
            )
        requested = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        candidate = {
            "schema_version": 1,
            "operation": "REVIEW_RETIREMENT",
            "retirement_id": retirement_id,
            "repository": repository,
            "project": self._project,
            "pr_number": self._pr,
            "commit": commit,
            "review_id": review_id,
            "source_path": _resolve(os.fspath(source)),
            "expected_sha256": expected_sha256,
            "reason": reason,
            "policy_id": policy_id,
            "requested_at": requested,
        }
        if envelope is None:
            self._atomic_create(envelope_path, _serialize(candidate))
            try:
                winner_raw, _winner_digest = self._read_bound_bytes(
                    envelope_path, "retirement publication winner"
                )
                envelope = self._require_retirement_envelope(
                    _parse_json_bytes(
                        winner_raw, envelope_path, "retirement publication winner"
                    ),
                    commit,
                    retirement_id,
                    bind_to_store=False,
                )
            except FileNotFoundError as error:
                raise MalformedEvidence(
                    "retirement envelope disappeared after publication"
                ) from error
            if envelope is None:
                raise MalformedEvidence("retirement envelope disappeared after publication")
            candidate_identity = {
                key: candidate[key]
                for key in _RETIREMENT_ENVELOPE_KEYS
                if key != "requested_at"
            }
            winner_identity = {
                key: envelope[key]
                for key in _RETIREMENT_ENVELOPE_KEYS
                if key != "requested_at"
            }
            if winner_identity != candidate_identity:
                raise EvidenceConflict(
                    "retirement envelope winner has different immutable arguments"
                )
            # The publication winner owns the source identity and all bytes.
            # Do not continue with any local caller state after a race.
            commit = envelope["commit"]
            retirement_id = envelope["retirement_id"]
            expected_sha256 = envelope["expected_sha256"]
            staging_path = self.retirement_staging_path(commit, retirement_id)
            final_path = self.retirement_final_path(commit, retirement_id)
            source = Path(envelope["source_path"])
            raw, actual_digest = self._read_bound_bytes(
                source, "retirement winner active review record"
            )
            if actual_digest != envelope["expected_sha256"]:
                raise EvidenceConflict(
                    "retirement winner source digest does not match its envelope"
                )
        if staging_path.exists() or staging_path.is_symlink():
            if not any(
                event.get("event_type") == RETIREMENT_COPIED_EVENT
                and event.get("retirement_id") == retirement_id
                and event.get("commit") == commit
                for event in self._read_authoritative_events()
            ):
                raise EvidenceConflict("pre-existing retirement staging lacks its matching COPIED event")
        self._append_retirement_transition(RETIREMENT_PREPARED_EVENT, envelope, commit, events)
        events = self._read_authoritative_events()
        staging = self.retirement_staging_path(commit, retirement_id)
        if not staging.exists():
            self._atomic_create(staging, raw)
        self._append_retirement_transition(RETIREMENT_COPIED_EVENT, envelope, commit, events)
        events = self._read_authoritative_events()
        if final_path.exists() and not any(
            event.get("event_type") == RETIREMENT_COMMITTED_EVENT
            and event.get("retirement_id") == retirement_id
            and event.get("commit") == commit
            for event in self._read_authoritative_events()
        ):
            raise EvidenceConflict("pre-existing retirement final lacks its matching COMMITTED event")
        if not final_path.exists():
            self._append_retirement_transition(RETIREMENT_COMMITTED_EVENT, envelope, commit, events)
            events = self._read_authoritative_events()
        self._move_no_replace(source, self.retirement_final_path(commit, retirement_id), expected_sha256)
        self._append_retirement_transition(RETIREMENT_FINALIZED_EVENT, envelope, commit, self._read_authoritative_events())
        return self.retirement_status(commit, retirement_id)

    def recover_retirement(self, retirement_id, commit) -> dict:
        with self._policy_adoption_lock():
            return self._recover_retirement_locked(retirement_id, commit)

    def _recover_retirement_locked(self, retirement_id, commit) -> dict:
        envelope = self._read_retirement_envelope(commit, retirement_id)
        if envelope is None:
            raise MalformedEvidence("retirement envelope does not exist")
        events = self._read_authoritative_events()
        original_review_events = [
            event
            for event in events
            if event["commit"] == commit
            and event["event_type"] == REVIEW_EVENT
            and _resolve(event["evidence_path"]) == envelope["source_path"]
            and event.get("content_digest") == envelope["expected_sha256"]
        ]
        if len(original_review_events) != 1:
            raise MalformedEvidence("retirement must bind exactly one original REVIEW_EVENT")
        source = Path(envelope["source_path"])
        staging = self.retirement_staging_path(commit, retirement_id)
        final = self.retirement_final_path(commit, retirement_id)

        def read_optional(path, label):
            if not path.exists() and not path.is_symlink():
                return None
            raw_value, digest_value = self._read_bound_bytes(path, label)
            if digest_value != envelope["expected_sha256"]:
                raise MalformedEvidence("{0} digest does not match its envelope".format(label))
            return raw_value

        raw = read_optional(source, "active review record")
        staging_raw = read_optional(staging, "retirement staging proof")
        read_optional(final, "retired review record")
        selected = self._retirement_events(commit, retirement_id, events)
        event_types = [event["event_type"] for event in selected]
        allowed_prefixes = [
            [],
            [RETIREMENT_PREPARED_EVENT],
            [RETIREMENT_PREPARED_EVENT, RETIREMENT_COPIED_EVENT],
            [RETIREMENT_PREPARED_EVENT, RETIREMENT_COPIED_EVENT, RETIREMENT_COMMITTED_EVENT],
            list(RETIREMENT_EVENT_TYPES),
        ]
        if event_types not in allowed_prefixes:
            raise MalformedEvidence("retirement events are not a valid state-machine prefix")
        for event_type, event in zip(event_types, selected):
            self._validate_retirement_event(event, envelope, event_type)

        if RETIREMENT_PREPARED_EVENT not in event_types:
            if raw is None and staging_raw is None:
                raise MalformedEvidence(
                    "retirement recovery lacks both active source and immutable staging bytes"
                )
            self._append_retirement_transition(
                RETIREMENT_PREPARED_EVENT, envelope, commit, events
            )
            events = self._read_authoritative_events()
            event_types.append(RETIREMENT_PREPARED_EVENT)

        if RETIREMENT_COPIED_EVENT not in event_types:
            if staging_raw is None:
                if raw is None:
                    raise MalformedEvidence(
                        "retirement recovery lacks bytes for the staging proof"
                    )
                self._atomic_create(staging, raw)
                staging_raw = raw
            self._append_retirement_transition(
                RETIREMENT_COPIED_EVENT, envelope, commit, self._read_authoritative_events()
            )
            event_types.append(RETIREMENT_COPIED_EVENT)

        if RETIREMENT_COMMITTED_EVENT not in event_types:
            if final.exists() or final.is_symlink():
                raise EvidenceConflict(
                    "retirement final exists before its matching COMMITTED event"
                )
            self._append_retirement_transition(
                RETIREMENT_COMMITTED_EVENT, envelope, commit, self._read_authoritative_events()
            )
            event_types.append(RETIREMENT_COMMITTED_EVENT)

        if RETIREMENT_FINALIZED_EVENT not in event_types:
            if not final.exists() and not final.is_symlink():
                if not source.exists():
                    raise MalformedEvidence(
                        "COMMITTED retirement has neither active source nor final artifact"
                    )
                self._move_no_replace(source, final, envelope["expected_sha256"])
            self._append_retirement_transition(
                RETIREMENT_FINALIZED_EVENT, envelope, commit, self._read_authoritative_events()
            )
        return self.retirement_status(commit, retirement_id)

    # -- event/artifact relation (T6L-F5) ---------------------------------

    VERIFICATION_RECORD_PHASES = frozenset({"local_review_ready", "closure_acceptance"})

    def _validate_all_retirements(self, commit, events):
        retirement_event_ids = []
        for event in events:
            if event["commit"] == commit and event["event_type"] in RETIREMENT_EVENT_TYPES:
                event_id = event.get("event_id")
                if not isinstance(event_id, str) or not event_id.strip():
                    raise MalformedEvidence("retirement events require non-blank event_id values")
                retirement_event_ids.append(event_id)
        if len(set(retirement_event_ids)) != len(retirement_event_ids):
            raise MalformedEvidence("retirement event IDs must be unique")
        retirement_ids = {
            event.get("retirement_id")
            for event in events
            if event["commit"] == commit and event["event_type"] in RETIREMENT_EVENT_TYPES
        }
        retirement_root = self._base / "retirements" / commit
        if retirement_root.exists():
            if retirement_root.is_symlink() or not retirement_root.is_dir():
                raise MalformedEvidence("retirement root must be a real directory")
            for directory in retirement_root.iterdir():
                if directory.is_symlink() or not directory.is_dir():
                    raise MalformedEvidence("retirement entry must be a real directory")
                retirement_ids.add(directory.name)
        for retirement_id in sorted(retirement_ids):
            state, envelope = self._retirement_state(commit, retirement_id, events)
            if envelope is None or state is None:
                raise MalformedEvidence("retirement event has no durable envelope")
            source = Path(envelope["source_path"])
            review_events = [
                event
                for event in events
                if event["commit"] == commit
                and event["event_type"] == REVIEW_EVENT
                and _resolve(event["evidence_path"]) == envelope["source_path"]
                and event.get("content_digest") == envelope["expected_sha256"]
            ]
            if len(review_events) != 1:
                raise MalformedEvidence("retirement must bind exactly one original REVIEW_EVENT")
            if state == "FINALIZED" and source.exists():
                raise MalformedEvidence("FINALIZED retirement still has its active source")

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
        events = self._read_authoritative_events()
        commit_events = [event for event in events if event["commit"] == commit]
        self._validate_all_retirements(commit, events)

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
                target = Path(event["evidence_path"])
                if target.exists():
                    self._require_artifact_target(event, commit, "reviews")
                else:
                    finalized = any(
                        retirement["state"] == "FINALIZED"
                        and retirement["source_path"] == _resolve(event["evidence_path"])
                        and retirement["expected_sha256"] == event["content_digest"]
                        for retirement in self._retirement_records(commit, events)
                    )
                    if not finalized:
                        raise MalformedEvidence(
                            "review artifact event target is missing without finalized retirement"
                        )
                review_by_path.setdefault(event["evidence_path"], []).append(event)
            elif event["event_type"] == POLICY_ACTIVATION_EVENT:
                if event.get("result") != "ELIGIBLE":
                    raise MalformedEvidence("policy activation event must be ELIGIBLE")
                staged_digest = event.get("staged_config_digest")
                projected_digest = event.get("projected_enforced_config_digest")
                _require_digest(staged_digest)
                _require_digest(projected_digest)
                expected_id = hashlib.sha256(
                    (commit + staged_digest + projected_digest).encode("ascii")
                ).hexdigest()
                if event.get("event_id") != expected_id:
                    raise MalformedEvidence("policy activation event id is not bound to its digests")

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

    def _retirement_records(self, commit, events=None):
        if events is None:
            events = self._read_authoritative_events()
        records = []
        retirement_ids = {
            event.get("retirement_id")
            for event in events
            if event["commit"] == commit and event["event_type"] in RETIREMENT_EVENT_TYPES
        }
        for retirement_id in sorted(retirement_ids):
            state, envelope = self._retirement_state(commit, retirement_id, events)
            if envelope is not None:
                records.append({"state": state, **envelope})
        return tuple(records)
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
        events = self._read_authoritative_events()
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
        retirements = self._retirement_records(commit)
        if any(item["state"] != "FINALIZED" for item in retirements):
            raise MalformedEvidence("review retirement is incomplete; active authority is revoked")
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
        try:
            return read_contained_file(target, self._root, label)
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence(f"cannot open {label}: {target}: {error}") from error

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
        with self._policy_adoption_lock():
            return self._commit_record_locked(
                target, line, event_type, commit, config_digest=config_digest
            )

    def _commit_record_locked(
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
        try:
            self.events_path.lstat()
        except FileNotFoundError:
            if self._base.exists():
                try:
                    if any(self._base.iterdir()):
                        raise MalformedEvidence(
                            "authoritative events file is missing; explicit stream migration is required"
                        )
                except OSError as error:
                    raise MalformedEvidence("cannot inspect the event store before record creation") from error
            self._append_line(b"")
        events = self._read_authoritative_events()
        created = self._atomic_create(target, line)
        if not created:
            existing = self._read_existing_for_compare(target)
            if existing != line:
                raise EvidenceConflict(
                    f"record already exists with different bytes: {target}"
                )
        content_digest = hashlib.sha256(line).hexdigest()
        if self._has_matching_event(
            events,
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
        try:
            return secure_atomic_create(target, line)
        except (OSError, SecurePathError) as error:
            raise MalformedEvidence("cannot atomically create durable record: {0}".format(target)) from error
