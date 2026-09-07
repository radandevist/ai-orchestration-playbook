from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from enum import StrEnum
from typing import BinaryIO, Callable, Mapping, Optional, Sequence, Tuple

from pr_closure.contract import COMMIT_ID_PATTERN, REPOSITORY_PATTERN
from pr_closure.jsonio import StrictJsonError, loads as strict_json_loads
from pr_closure.model import CiState, MergeableState

_COMMIT_ID_RE = re.compile(COMMIT_ID_PATTERN)
_REPOSITORY_RE = re.compile(REPOSITORY_PATTERN)
_NON_BLANK_RE = re.compile(r"\S")
_WHITESPACE_RE = re.compile(r"\s")
MAX_SNAPSHOT_ARTIFACT_BYTES = 64 * 1024
MAX_COLLECTION_ITEMS = 1000
MAX_COLLECTION_PAGES = 10
MAX_RUN_ATTEMPTS = 20

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SourceError(ValueError):
    """Base class for fail-closed live-source read failures."""


class SourceUnavailable(SourceError):
    """Raised when a command fails, times out, cannot be run, or its runner
    misbehaves. Never a favorable default; the caller must fail closed."""


class SourceMalformed(SourceError):
    """Raised when command output is empty, whitespace-only, malformed JSON,
    the wrong shape, missing required keys, or carries an unknown value."""


# ---------------------------------------------------------------------------
# Diagnostics (command context + stderr, never secrets)
# ---------------------------------------------------------------------------
#
# T4R-N1 (fully closed): redaction covers URL credentials
# (http(s)/ssh/git/sftp), the ``Authorization: Bearer <secret>`` header,
# ``KEY=value`` pairs (unconstrained value), secret-key forms separated by
# ``:`` or ``=`` (value redacted unconditionally, regardless of key case,
# token alphabet, or length), secret-key whitespace-separated forms (redacted
# only for non-lowercase keys with token-shaped values, so prose such as
# ``token is used`` survives), and secret flags (``--token <value>`` and
# ``--token=<value>``, value redacted unconditionally). Prose with no secret
# separator or flag value, such as ``token is used for auth``, is never
# erased. The unconstrained ``KEY=value`` and ``Authorization: Bearer`` forms
# stay unconstrained because they are pinned by the pre-existing and Task 6
# tests.

_CREDENTIALS_IN_URL_RE = re.compile(r"(?i)(?P<scheme>https?://|ssh://|git://|sftp://)[^/@\s]+@")
_SECRET_KV_RE = re.compile(r"(?i)(password|token|secret|api[_-]?key|access[_-]?token|auth[a-z_-]*)=([^&\s]+)")
_AUTHORIZATION_BEARER_RE = re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)\S+")
_SECRET_KEY_SEPARATOR_RE = re.compile(
    r"(?i)(?P<key>gh[_-]?token|github[_-]?token|access[_-]?token|api[_-]?key|"
    r"auth[_-]?token|auth[_-]?key|password|secret|token|bearer)"
    r"(?P<sep>\s*[:=]\s*)(?P<value>[^\s,*]+)"
)
_SECRET_KEY_SPACE_RE = re.compile(
    r"(?i)(?P<key>gh[_-]?token|github[_-]?token|access[_-]?token|api[_-]?key|"
    r"auth[_-]?token|auth[_-]?key|password|secret|token|bearer)"
    r"(?P<sep>\s+)(?P<value>[^\s,*]+)"
)
_SECRET_FLAG_RE = re.compile(
    r"(?i)(?P<flag>--(?:password|token|secret|api[_-]?key|access[_-]?token|"
    r"auth[_-]?token|gh[_-]?token))(?P<sep>[=\s]+)(?P<value>[^\s,*]+)"
)
_UNSAFE_VALUE_CHARS = frozenset("0123456789_/.-+=")


def _looks_like_secret(value: str) -> bool:
    if len(value) >= 8:
        return True
    return len(value) >= 4 and any(ch in _UNSAFE_VALUE_CHARS for ch in value)


def _redact_separated(match) -> str:
    return match.group("key") + match.group("sep") + "***"


def _redact_space_separated(match) -> str:
    key = match.group("key")
    if key.islower():
        return match.group(0)
    value = match.group("value")
    if not _looks_like_secret(value):
        return match.group(0)
    return key + match.group("sep") + "***"


def _redact_flag(match) -> str:
    return match.group("flag") + match.group("sep") + "***"


def redact(text: str) -> str:
    """Redact credentials and secret carriers from a diagnostic string.

    Used for command displays and stderr so secrets never persist into CLI
    diagnostics. Preserves surrounding context and avoids erasing prose that
    carries no secret separator or flag value.
    """
    text = _CREDENTIALS_IN_URL_RE.sub(r"\g<scheme>***@", text)
    text = _AUTHORIZATION_BEARER_RE.sub(r"\1***", text)
    text = _SECRET_KV_RE.sub(lambda match: match.group(1) + "=***", text)
    text = _SECRET_KEY_SEPARATOR_RE.sub(_redact_separated, text)
    text = _SECRET_KEY_SPACE_RE.sub(_redact_space_separated, text)
    text = _SECRET_FLAG_RE.sub(_redact_flag, text)
    return text


def _describe_command(argv: Sequence[str]) -> str:
    return redact(" ".join(str(part) for part in argv))


# ---------------------------------------------------------------------------
# Command runner
# ---------------------------------------------------------------------------

CommandResult = Tuple[int, str, str]
Runner = Callable[[Sequence[str], Optional[float]], CommandResult]
BinaryCommandResult = Tuple[int, str]
ArtifactRunner = Callable[[Sequence[str], Optional[float], BinaryIO, int], BinaryCommandResult]


def default_runner(argv: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
    """Run ``argv`` as a list with no shell; never interpolate user input."""
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return (proc.returncode, proc.stdout, proc.stderr)


def default_artifact_runner(
    argv: Sequence[str],
    timeout: Optional[float],
    output_path: BinaryIO,
    max_bytes: int,
) -> BinaryCommandResult:
    """Stream binary stdout into a bounded caller-owned file without decoding it.

    ``gh api`` follows the REST artifact redirect through its HTTP client. The
    runner deliberately leaves redirect handling to that supported client path;
    it only owns bounded byte streaming and process supervision.
    """
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        close_fds=True,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stderr = bytearray()
    written = 0
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(argv, timeout)
            ready = selector.select(remaining)
            if not ready:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in ready:
                chunk = key.fileobj.read1(64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    written += len(chunk)
                    if written > max_bytes:
                        process.kill()
                        process.wait()
                        raise SourceMalformed("snapshot archive exceeds the byte limit")
                    output_path.write(chunk)
                elif len(stderr) < 64 * 1024:
                    stderr.extend(chunk[: 64 * 1024 - len(stderr)])
        if deadline is None:
            returncode = process.wait()
        else:
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
    except (BrokenPipeError, OSError):
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return returncode, stderr.decode("utf-8", errors="replace")


def _invoke(argv: Tuple[str, ...], timeout, runner: Runner) -> CommandResult:
    try:
        result = runner(argv, timeout)
    except (subprocess.TimeoutExpired, TimeoutError) as error:
        raise SourceUnavailable(
            "source command timed out after {0!r}s: {1}".format(timeout, _describe_command(argv))
        ) from error
    except OSError as error:
        raise SourceUnavailable(
            "cannot run source command {0}: {1}".format(
                _describe_command(argv), redact(str(error))
            )
        ) from error
    if not isinstance(result, tuple) or len(result) != 3:
        raise SourceMalformed(
            "command runner returned an unexpected shape for: {0}".format(
                _describe_command(argv)
            )
        )
    returncode, stdout, stderr = result
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise SourceMalformed(
            "command runner returned a non-integer returncode for: {0}".format(
                _describe_command(argv)
            )
        )
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise SourceMalformed(
            "command runner returned non-text output for: {0}".format(
                _describe_command(argv)
            )
        )
    if returncode != 0:
        if returncode < 0:
            detail = "did not complete (timeout or signal)"
        else:
            detail = "exited with status {0}".format(returncode)
        raise SourceUnavailable(
            "source command {0} {1}; stderr: {2}".format(
                _describe_command(argv), detail, redact(stderr)
            )
        )
    return (returncode, stdout, stderr)


def _require_non_blank(stdout: str, argv, label: str) -> str:
    if not stdout:
        raise SourceMalformed("{0} returned empty output for: {1}".format(label, _describe_command(argv)))
    if not stdout.strip():
        raise SourceMalformed("{0} returned only whitespace for: {1}".format(label, _describe_command(argv)))
    return stdout


def _parse_json_object(stdout: str, argv, label: str) -> dict:
    raw = _require_non_blank(stdout, argv, label)
    try:
        parsed = strict_json_loads(raw, label)
    except StrictJsonError as error:
        raise SourceMalformed(
            "{0} returned malformed JSON for: {1}: {2}".format(label, _describe_command(argv), error)
        ) from error
    if not isinstance(parsed, dict):
        raise SourceMalformed("{0} must be a JSON object for: {1}".format(label, _describe_command(argv)))
    return parsed


def _parse_json_value(stdout: str, argv, label: str):
    raw = _require_non_blank(stdout, argv, label)
    try:
        return strict_json_loads(raw, label)
    except StrictJsonError as error:
        raise SourceMalformed(
            "{0} returned malformed JSON for: {1}: {2}".format(label, _describe_command(argv), error)
        ) from error


def _require_commit(value, label: str, argv) -> str:
    if not isinstance(value, str) or _COMMIT_ID_RE.fullmatch(value) is None:
        raise SourceMalformed(
            "{0} must be a 40-character lowercase hexadecimal id for: {1}".format(
                label, _describe_command(argv)
            )
        )
    return value


def _require_commit_output(stdout: str, argv, label: str) -> str:
    return _require_commit(
        _require_non_blank(stdout, argv, label).strip(), label, argv
    )


# ---------------------------------------------------------------------------
# Git worktree parsing
# ---------------------------------------------------------------------------

_WORKTREE_FIELD_RE = re.compile(r"^([A-Za-z_]+)(?: (.*))?$")


@dataclass(frozen=True)
class WorktreeRecord:
    path: Optional[str]  # resolved absolute path; None for a bare repository
    head: Optional[str]  # 40-char lowercase hex; None for a bare repository
    branch: Optional[str]  # checked-out branch name without refs/heads/
    detached: bool
    bare: bool


def _split_blocks(stdout: str) -> Tuple[Tuple[str, ...], ...]:
    blocks = []
    current = []
    for line in stdout.splitlines():
        if line == "":
            if current:
                blocks.append(tuple(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(tuple(current))
    return tuple(blocks)


def _parse_worktree_block(lines, argv) -> WorktreeRecord:
    if not lines:
        raise SourceMalformed("empty worktree record for: {0}".format(_describe_command(argv)))
    marker = lines[0]
    if marker == "bare":
        bare, path, rest = True, None, lines[1:]
    elif marker.startswith("worktree "):
        path = marker[len("worktree "):]
        if not path or not os.path.isabs(path):
            raise SourceMalformed(
                "worktree record path must be absolute for: {0}".format(_describe_command(argv))
            )
        bare, rest = False, lines[1:]
    else:
        raise SourceMalformed(
            "unexpected worktree marker {0!r} for: {1}".format(marker, _describe_command(argv))
        )
    head = None
    branch = None
    detached = False
    for line in rest:
        match = _WORKTREE_FIELD_RE.match(line)
        if match is None:
            raise SourceMalformed(
                "malformed worktree field {0!r} for: {1}".format(line, _describe_command(argv))
            )
        key, value = match.groups()
        key = key.lower()
        if key == "head":
            if head is not None:
                raise SourceMalformed(
                    "duplicate worktree HEAD field for: {0}".format(_describe_command(argv))
                )
            head = value
        elif key == "branch":
            if branch is not None or detached:
                raise SourceMalformed(
                    "contradictory worktree branch fields for: {0}".format(_describe_command(argv))
                )
            if value is None or not value.startswith("refs/heads/"):
                raise SourceMalformed(
                    "worktree branch must be a refs/heads reference for: {0}".format(
                        _describe_command(argv)
                    )
                )
            branch = value[len("refs/heads/"):]
        elif key == "detached":
            if value is not None or branch is not None:
                raise SourceMalformed(
                    "contradictory worktree detached/branch fields for: {0}".format(
                        _describe_command(argv)
                    )
                )
            detached = True
        elif key == "bare":
            if value is not None or bare:
                raise SourceMalformed(
                    "contradictory worktree bare field for: {0}".format(_describe_command(argv))
                )
            bare = True
        elif key in ("prunable", "reftable", "locked", "lock_reason"):
            continue
        else:
            raise SourceMalformed(
                "unknown worktree field {0!r} for: {1}".format(key, _describe_command(argv))
            )
    if bare:
        if head is not None or branch is not None or detached:
            raise SourceMalformed(
                "contradictory bare worktree record for: {0}".format(_describe_command(argv))
            )
        path = None
    elif head is None:
        raise SourceMalformed(
            "worktree record missing HEAD for: {0}".format(_describe_command(argv))
        )
    else:
        head = _require_commit(head, "worktree HEAD", argv)
    resolved = None
    if path is not None:
        resolved = os.path.realpath(os.path.abspath(path))
    return WorktreeRecord(path=resolved, head=head, branch=branch, detached=detached, bare=bare)


# ---------------------------------------------------------------------------
# GitSource
# ---------------------------------------------------------------------------

WORKTREE_LIST_COMMAND = ("worktree", "list", "--porcelain")
STATUS_COMMAND = ("status", "--porcelain=v1", "--untracked-files=all")


@dataclass(frozen=True)
class GitFacts:
    worktree_path: str
    branch: str
    checked_out_branch: Optional[str]
    local_commit: str
    remote_commit: str
    worktree_clean: bool
    dirty_entries: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "dirty_entries", tuple(self.dirty_entries))


class GitSource:
    """Fail-closed reader for one Git worktree's local closure facts.

    Every read re-binds the requested absolute resolved path to exactly one
    ``git worktree list --porcelain`` record before using its path for ``-C``,
    so a moved, removed, duplicated, or contradictory worktree never yields a
    favorable default.
    """

    def __init__(self, worktree, branch, runner: Optional[Runner] = None, timeout=None):
        expanded = os.path.expanduser(os.fspath(worktree))
        if not os.path.isabs(expanded):
            raise SourceMalformed("worktree path must be absolute")
        self._resolved = os.path.realpath(expanded)
        if not isinstance(branch, str) or not branch.strip() or _WHITESPACE_RE.search(branch):
            raise SourceMalformed("branch must be a non-empty string without whitespace")
        self._branch = branch
        self._runner = runner or default_runner
        self._timeout = timeout

    @property
    def worktree_path(self) -> str:
        return self._resolved

    @property
    def branch(self) -> str:
        return self._branch

    def _git_c(self, worktree_path: str, sub_args) -> Tuple[str, ...]:
        return ("git", "-C", worktree_path) + tuple(sub_args)

    def discover_worktree(self) -> WorktreeRecord:
        argv = self._git_c(self._resolved, WORKTREE_LIST_COMMAND)
        _, stdout, _ = _invoke(argv, self._timeout, self._runner)
        raw = _require_non_blank(stdout, argv, "git worktree list")
        records = [_parse_worktree_block(block, argv) for block in _split_blocks(raw)]
        matches = [
            record
            for record in records
            if not record.bare and record.path == self._resolved
        ]
        if not matches:
            raise SourceMalformed(
                "requested worktree {0} is not among the {1} recorded worktree(s) for: {2}".format(
                    self._resolved, len(records), _describe_command(argv)
                )
            )
        if len(matches) > 1:
            raise SourceMalformed(
                "duplicate/contradictory worktree records bind {0} for: {1}".format(
                    self._resolved, _describe_command(argv)
                )
            )
        return matches[0]

    def local_head(self) -> str:
        record = self.discover_worktree()
        argv = self._git_c(record.path, ("rev-parse", "HEAD"))
        _, stdout, _ = _invoke(argv, self._timeout, self._runner)
        return _require_commit_output(stdout, argv, "git rev-parse HEAD")

    def remote_head(self) -> str:
        record = self.discover_worktree()
        argv = self._git_c(record.path, ("rev-parse", "origin/{0}".format(self._branch)))
        _, stdout, _ = _invoke(argv, self._timeout, self._runner)
        return _require_commit_output(stdout, argv, "git rev-parse origin/{0}".format(self._branch))

    def worktree_clean(self) -> Tuple[bool, Tuple[str, ...]]:
        record = self.discover_worktree()
        argv = self._git_c(record.path, STATUS_COMMAND)
        _, stdout, _ = _invoke(argv, self._timeout, self._runner)
        if stdout == "":
            return (True, ())
        if not stdout.strip():
            raise SourceMalformed(
                "git status returned only whitespace for: {0}".format(_describe_command(argv))
            )
        lines = tuple(stdout.splitlines())
        if any(not line.strip() for line in lines):
            raise SourceMalformed(
                "git status emitted a blank entry for: {0}".format(_describe_command(argv))
            )
        return (False, lines)

    def facts(self) -> GitFacts:
        record = self.discover_worktree()
        if record.branch != self._branch:
            raise SourceMalformed(
                "worktree {0} is checked out on branch {1!r}, expected {2!r} for: {3}".format(
                    record.path, record.branch, self._branch, _describe_command(
                        self._git_c(record.path, WORKTREE_LIST_COMMAND)
                    )
                )
            )
        local = self.local_head()
        remote = self.remote_head()
        clean, entries = self.worktree_clean()
        return GitFacts(
            worktree_path=record.path,
            branch=self._branch,
            checked_out_branch=record.branch,
            local_commit=local,
            remote_commit=remote,
            worktree_clean=clean,
            dirty_entries=entries,
        )


class WorktreeResolver:
    """Fail-closed resolver of the live PR worktree from a repository anchor.

    ``repo_path`` is the repository anchor (the configured main checkout, for
    example on the PR base branch). The resolver runs ``git worktree list
    --porcelain`` from the anchor and resolves exactly one non-bare worktree
    whose checked-out branch equals the live PR head branch. It never
    constructs a worktree path: the path is always the one git reported.
    Missing, detached, duplicate, or mismatched branch bindings raise a typed
    source error so callers fail closed before any evidence write.
    """

    def __init__(self, anchor, runner: Optional[Runner] = None, timeout=None):
        expanded = os.path.expanduser(os.fspath(anchor))
        if not os.path.isabs(expanded):
            raise SourceMalformed("repository anchor path must be absolute")
        self._resolved = os.path.realpath(expanded)
        self._runner = runner or default_runner
        self._timeout = timeout

    @property
    def anchor(self) -> str:
        return self._resolved

    def resolve(self, branch: str) -> WorktreeRecord:
        if not isinstance(branch, str) or not branch.strip() or _WHITESPACE_RE.search(branch):
            raise SourceMalformed("branch must be a non-empty string without whitespace")
        argv = ("git", "-C", self._resolved) + WORKTREE_LIST_COMMAND
        _, stdout, _ = _invoke(argv, self._timeout, self._runner)
        raw = _require_non_blank(stdout, argv, "git worktree list")
        records = [_parse_worktree_block(block, argv) for block in _split_blocks(raw)]
        matches = [record for record in records if not record.bare and record.branch == branch]
        if not matches:
            raise SourceMalformed(
                "no recorded worktree checks out branch {0!r} among {1} worktree(s) for: {2}".format(
                    branch, len(records), _describe_command(argv)
                )
            )
        if len(matches) > 1:
            raise SourceMalformed(
                "duplicate/contradictory worktree records check out branch {0!r} for: {1}".format(
                    branch, _describe_command(argv)
                )
            )
        return matches[0]


# ---------------------------------------------------------------------------
# GitHub PR reading
# ---------------------------------------------------------------------------

PR_JSON_FIELDS = (
    "number,headRefName,headRefOid,isDraft,state,mergeStateStatus,mergeable,"
    "statusCheckRollup,url,baseRefName,body,potentialMergeCommit"
)
_REQUIRED_PR_KEYS = (
    "number",
    "headRefName",
    "headRefOid",
    "isDraft",
    "state",
    "mergeStateStatus",
    "mergeable",
    "statusCheckRollup",
    "url",
    "baseRefName",
    "body",
    "potentialMergeCommit",
)
PR_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
PR_CHECK_NODE_TYPES = frozenset({"CheckRun", "StatusContext", "CheckSuite"})
CHECK_RUN_STATUSES = frozenset({"QUEUED", "IN_PROGRESS", "COMPLETED", "PENDING", "REQUESTED", "WAITING"})
CHECK_CONCLUSIONS = frozenset({
    "SUCCESS",
    "FAILURE",
    "NEUTRAL",
    "CANCELLED",
    "SKIPPED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "STALE",
})
STATUS_CONTEXT_STATES = frozenset({"SUCCESS", "FAILURE", "PENDING", "EXPECTED", "ERROR"})


class CheckOutcome(StrEnum):
    PASSING = "PASSING"
    PENDING = "PENDING"
    FAILURE = "FAILURE"
    SKIPPED = "SKIPPED"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class CheckResult:
    node_type: str  # "check_run" | "status_context" | "check_suite"
    name: str
    status: str  # raw status/state string from GitHub
    conclusion: Optional[str]  # raw conclusion; None when not completed
    outcome: CheckOutcome
    details_url: Optional[str] = None
    check_run_id: Optional[int] = None
    head_sha: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    app_slug: Optional[str] = None
    check_suite_id: Optional[int] = None
    workflow_run_id: Optional[int] = None
    workflow_id: Optional[int] = None
    workflow_path: Optional[str] = None
    workflow_action: Optional[str] = None
    workflow_event: Optional[str] = None
    run_attempt: Optional[int] = None


@dataclass(frozen=True)
class CheckRunCandidate:
    result: Optional[CheckResult]
    name: Optional[str] = None
    error: Optional[str] = None
    authoritative: bool = True

    def __post_init__(self):
        if self.name is None and self.result is not None:
            object.__setattr__(self, "name", self.result.name)


@dataclass(frozen=True)
class LivePrSnapshot:
    pr_number: int
    head_sha: str
    base_ref_name: str
    potential_merge_commit_oid: str
    body_sha256: str
    is_draft: bool
    event_name: str
    event_sha: str
    workflow_path: str
    workflow_id: int
    workflow_action: str
    run_id: int
    run_attempt: int

    def matches_live(
        self,
        *,
        pr_number: int,
        head_sha: str,
        base_ref_name: str,
        potential_merge_commit_oid: str,
        body: str,
        is_draft: bool,
        workflow_path: str,
        workflow_id: int,
        workflow_action: str,
        run_id: int,
        run_attempt: int,
    ) -> bool:
        return (
            self.pr_number == pr_number
            and self.head_sha == head_sha
            and self.base_ref_name == base_ref_name
            and self.potential_merge_commit_oid == potential_merge_commit_oid
            and self.body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
            and self.is_draft is is_draft
            and self.event_name == "pull_request"
            and self.event_sha == potential_merge_commit_oid
            and self.workflow_path == workflow_path
            and self.workflow_id == workflow_id
            and self.workflow_action == workflow_action
            and self.run_id == run_id
            and self.run_attempt == run_attempt
        )


@dataclass(frozen=True)
class PullRequestFacts:
    repository: str
    number: int
    head_branch: str
    base_ref_name: str
    head_oid: str
    is_draft: bool
    state: str
    merge_state_status: str
    mergeable: MergeableState
    url: str
    checks: Tuple[CheckResult, ...]
    body: str = ""
    potential_merge_commit_oid: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "checks", tuple(self.checks))


@dataclass(frozen=True)
class CiFacts:
    ci_state: CiState
    checks: Tuple[CheckResult, ...]
    ci_commit: str
    infra_job: Optional[str] = None
    reasons: Tuple[str, ...] = ()
    check_run_id: Optional[int] = None
    check_suite_id: Optional[int] = None
    head_sha: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    app_slug: Optional[str] = None
    base_ref_name: Optional[str] = None
    potential_merge_commit_oid: Optional[str] = None
    event_sha: Optional[str] = None
    workflow_path: Optional[str] = None
    workflow_id: Optional[int] = None
    workflow_action: Optional[str] = None
    workflow_event: Optional[str] = None
    workflow_run_id: Optional[int] = None
    run_attempt: Optional[int] = None
    snapshot_body_sha256: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "reasons", tuple(self.reasons))


def _timestamp_key(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def select_check_run_candidates(
    candidates: Sequence[CheckRunCandidate],
    required_checks: Sequence[str],
    *,
    head_oid: str,
) -> CiFacts:
    """Select exact-head check runs deterministically, fail closed on ambiguity."""
    head_oid = _require_commit(head_oid, "PR head", ())
    selected = []
    failures = []
    pending = []
    reasons = []
    for name in tuple(required_checks):
        matching = [candidate for candidate in candidates if candidate.name == name]
        matching = [candidate for candidate in matching if candidate.authoritative]
        if not matching:
            reasons.append(
                "required check {0} missing from current workflow attempt".format(name)
            )
            continue
        malformed = [candidate.error for candidate in matching if candidate.error]
        if malformed:
            reasons.append(
                "required check {0} has malformed candidate: {1}".format(
                    name, "; ".join(str(error) for error in malformed)
                )
            )
            continue
        results = [candidate.result for candidate in matching]
        if any(result is None for result in results):
            reasons.append("required check {0} has malformed candidate".format(name))
            continue
        check_ids = [result.check_run_id for result in results]
        if len(set(check_ids)) != len(check_ids):
            reasons.append(
                "required check {0} has duplicate check-run identity".format(name)
            )
            continue
        try:
            starts = [_timestamp_key(result.started_at) for result in results]
        except (TypeError, ValueError) as error:
            reasons.append("required check {0} has malformed candidate: {1}".format(name, error))
            continue
        latest_start = max(starts)
        latest = [
            result for result, started in zip(results, starts) if started == latest_start
        ]
        suites = {result.check_suite_id for result in latest}
        if len(suites) > 1:
            reasons.append(
                "required check {0} has ambiguous concurrent runs".format(name)
            )
            continue
        try:
            selected_result = max(
                latest,
                key=lambda result: (
                    _timestamp_key(result.completed_at)
                    if result.completed_at is not None
                    else datetime.min.replace(tzinfo=latest_start.tzinfo),
                    result.check_run_id or -1,
                ),
            )
        except (TypeError, ValueError) as error:
            reasons.append("required check {0} has malformed candidate: {1}".format(name, error))
            continue
        selected.append(selected_result)
        if selected_result.head_sha != head_oid:
            reasons.append(
                "required check {0} candidate head does not match PR head".format(name)
            )
        elif selected_result.status != "COMPLETED":
            pending.append(name)
            reasons.append("required check {0} is pending".format(name))
        elif selected_result.outcome is CheckOutcome.PASSING:
            continue
        elif selected_result.outcome is CheckOutcome.FAILURE:
            failures.append(name)
            reasons.append("required check {0} failed".format(name))
        else:
            reasons.append(
                "required check {0} completed without SUCCESS".format(name)
            )
    if failures:
        state = CiState.BRANCH_FAILURE
    elif pending or reasons:
        state = CiState.UNKNOWN
    elif selected and len(selected) == len(tuple(required_checks)):
        state = CiState.PASSING
    else:
        state = CiState.UNKNOWN
    first = selected[0] if selected else None
    return CiFacts(
        ci_state=state,
        checks=tuple(selected),
        ci_commit=head_oid,
        reasons=tuple(reasons) if reasons else ("all required checks passing",),
        check_run_id=first.check_run_id if first else None,
        check_suite_id=first.check_suite_id if first else None,
        head_sha=first.head_sha if first else None,
        started_at=first.started_at if first else None,
        completed_at=first.completed_at if first else None,
        app_slug=first.app_slug if first else None,
        workflow_path=first.workflow_path if first else None,
        workflow_id=first.workflow_id if first else None,
        workflow_action=first.workflow_action if first else None,
        workflow_event=first.workflow_event if first else None,
        workflow_run_id=first.workflow_run_id if first else None,
        run_attempt=first.run_attempt if first else None,
    )


def _classify_check(node_type: str, status: str, conclusion) -> CheckOutcome:
    if node_type == "status_context":
        if status == "SUCCESS":
            return CheckOutcome.PASSING
        if status in ("FAILURE", "ERROR"):
            return CheckOutcome.FAILURE
        if status in ("PENDING", "EXPECTED"):
            return CheckOutcome.PENDING
        raise SourceMalformed("unsupported status context state: {0!r}".format(status))
    if status == "COMPLETED":
        if conclusion == "SUCCESS":
            return CheckOutcome.PASSING
        if conclusion in ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "STALE", "CANCELLED"):
            return CheckOutcome.FAILURE
        if conclusion == "SKIPPED":
            return CheckOutcome.SKIPPED
        if conclusion == "NEUTRAL":
            return CheckOutcome.NEUTRAL
        if conclusion == "ACTION_REQUIRED":
            return CheckOutcome.PENDING
        raise SourceMalformed("unsupported check conclusion: {0!r}".format(conclusion))
    return CheckOutcome.PENDING  # QUEUED/IN_PROGRESS/PENDING/REQUESTED/WAITING


def _require_non_empty_str(value, label: str, argv) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceMalformed("{0} must be a non-empty string for: {1}".format(label, _describe_command(argv)))
    return value


def parse_check(node, argv=()) -> CheckResult:
    if not isinstance(node, dict):
        raise SourceMalformed(
            "statusCheckRollup node must be a JSON object for: {0}".format(_describe_command(argv))
        )
    node_type = node.get("__typename")
    if not isinstance(node_type, str) or node_type not in PR_CHECK_NODE_TYPES:
        raise SourceMalformed(
            "unknown status check node type {0!r} for: {1}".format(node_type, _describe_command(argv))
        )
    kind = {
        "CheckRun": "check_run",
        "StatusContext": "status_context",
        "CheckSuite": "check_suite",
    }[node_type]
    details_url = node.get("detailsUrl")
    if details_url is not None and not isinstance(details_url, str):
        raise SourceMalformed(
            "detailsUrl must be a string for: {0}".format(_describe_command(argv))
        )
    if node_type == "StatusContext":
        name = _require_non_empty_str(node.get("context"), "status context", argv)
        state = node.get("state")
        if not isinstance(state, str) or state not in STATUS_CONTEXT_STATES:
            raise SourceMalformed(
                "unsupported status context state {0!r} for: {1}".format(state, _describe_command(argv))
            )
        outcome = _classify_check("status_context", state, None)
        if details_url is None:
            target_url = node.get("targetUrl")
            if target_url is not None and not isinstance(target_url, str):
                raise SourceMalformed(
                    "targetUrl must be a string for: {0}".format(_describe_command(argv))
                )
            details_url = target_url
        return CheckResult(kind, name, state, None, outcome, details_url)
    if node_type == "CheckSuite":
        app = node.get("app")
        if not isinstance(app, dict):
            raise SourceMalformed(
                "CheckSuite node requires an app mapping for: {0}".format(_describe_command(argv))
            )
        name = app.get("name") or app.get("slug")
        name = _require_non_empty_str(name, "check suite app name", argv)
    else:
        name = _require_non_empty_str(node.get("name"), "check run name", argv)
    status = node.get("status")
    if not isinstance(status, str) or status not in CHECK_RUN_STATUSES:
        raise SourceMalformed(
            "unsupported check status {0!r} for: {1}".format(status, _describe_command(argv))
        )
    conclusion = node.get("conclusion")
    # `gh pr view --json statusCheckRollup` serializes an unfinished check's
    # absent conclusion as "" rather than null. Preserve fail-closed handling
    # for completed checks, but normalize that transport quirk while the check
    # is still pending.
    if conclusion == "" and status != "COMPLETED":
        conclusion = None
    if conclusion is not None and (
        not isinstance(conclusion, str) or conclusion not in CHECK_CONCLUSIONS
    ):
        raise SourceMalformed(
            "unsupported check conclusion {0!r} for: {1}".format(conclusion, _describe_command(argv))
        )
    if status == "COMPLETED" and conclusion is None:
        raise SourceMalformed(
            "completed check requires a conclusion for: {0}".format(_describe_command(argv))
        )
    outcome = _classify_check(kind, status, conclusion)
    return CheckResult(kind, name, status, conclusion, outcome, details_url)


def _require_github_url(value, repository: str, number: int, kind: str, argv) -> str:
    if kind not in ("pull", "issues"):
        raise AssertionError("unknown github url kind: {0!r}".format(kind))
    if not isinstance(value, str) or not value.strip():
        raise SourceMalformed("PR url must be a non-empty string for: {0}".format(_describe_command(argv)))
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise SourceMalformed(
            "PR url must be a well-formed URL for: {0}: {1}".format(
                _describe_command(argv), error
            )
        ) from error
    if parsed.scheme != "https":
        raise SourceMalformed(
            "PR url must use https for: {0}".format(_describe_command(argv))
        )
    if parsed.username is not None or parsed.password is not None:
        raise SourceMalformed(
            "PR url must not carry credentials for: {0}".format(_describe_command(argv))
        )
    if port is not None and port != 443:
        raise SourceMalformed(
            "PR url must use the default https port for: {0}".format(_describe_command(argv))
        )
    if parsed.hostname != "github.com":
        raise SourceMalformed(
            "PR url must be on the github.com origin for: {0}".format(_describe_command(argv))
        )
    if parsed.query or parsed.fragment:
        raise SourceMalformed(
            "PR url must not carry a query or fragment for: {0}".format(_describe_command(argv))
        )
    expected = "/{0}/{1}/{2}".format(repository, kind, number)
    if parsed.path not in (expected, expected + "/"):
        raise SourceMalformed(
            "PR url must bind exactly to {0} {1} {2} for: {3}".format(
                repository, kind, number, _describe_command(argv)
            )
        )
    return value


def _require_pr_url(value, repository: str, number: int, argv) -> str:
    return _require_github_url(value, repository, number, "pull", argv)


def _require_positive_int(value, label: str, argv) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SourceMalformed(
            "{0} must be a positive integer for: {1}".format(label, _describe_command(argv))
        )
    return value


def _require_nonnegative_int(value, label: str, argv) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SourceMalformed("{0} must be a non-negative integer for: {1}".format(label, _describe_command(argv)))
    return value


def _read_explicit_pages(
    source: "GitHubSource",
    endpoint: str,
    item_key: str,
    item_label: str,
) -> Tuple[Mapping, ...]:
    """Read REST pages with only flags supported by gh 2.46.0."""
    total_count = None
    seen_ids = set()
    seen_pages = set()
    items = []
    page = 1
    while total_count is None or len(seen_ids) < total_count:
        if page > MAX_COLLECTION_PAGES:
            raise SourceMalformed(
                "{0} requires more than {1} pages".format(
                    item_label, MAX_COLLECTION_PAGES
                )
            )
        separator = "&" if "?" in endpoint else "?"
        page_endpoint = "{0}{1}page={2}&per_page=100".format(endpoint, separator, page)
        argv = ("gh", "api", page_endpoint)
        data = source._api_json(page_endpoint, label=item_label)
        raw_items = data.get(item_key)
        if not isinstance(raw_items, list):
            raise SourceMalformed("{0} page is malformed".format(item_label))
        page_total = _require_nonnegative_int(data.get("total_count"), "{0} total_count".format(item_label), argv)
        if page_total > MAX_COLLECTION_ITEMS:
            raise SourceMalformed(
                "{0} total_count exceeds {1} items".format(
                    item_label, MAX_COLLECTION_ITEMS
                )
            )
        if len(raw_items) > MAX_COLLECTION_ITEMS or len(items) + len(raw_items) > MAX_COLLECTION_ITEMS:
            raise SourceMalformed(
                "{0} collection exceeds {1} items".format(
                    item_label, MAX_COLLECTION_ITEMS
                )
            )
        if total_count is None:
            total_count = page_total
        elif page_total != total_count:
            raise SourceMalformed("{0} pages disagree on total_count".format(item_label))
        page_ids = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise SourceMalformed("{0} contains a non-object item".format(item_label))
            item_id = _require_positive_int(item.get("id"), "{0} id".format(item_label), argv)
            if item_id in seen_ids:
                raise SourceMalformed("{0} pages contain duplicate ids".format(item_label))
            seen_ids.add(item_id)
            page_ids.append(item_id)
            items.append(item)
        page_identity = tuple(page_ids)
        if page_identity in seen_pages:
            raise SourceMalformed("{0} pagination replayed a page".format(item_label))
        seen_pages.add(page_identity)
        if not raw_items:
            if total_count == len(seen_ids):
                break
            raise SourceMalformed("{0} pagination is truncated".format(item_label))
        if total_count == len(seen_ids):
            break
        page += 1
    if total_count != len(seen_ids):
        raise SourceMalformed("{0} pagination is truncated or over-counted".format(item_label))
    return tuple(items)


def _parse_actions_run_id(details_url: str, repository: str, argv) -> int:
    if not isinstance(details_url, str) or not details_url.strip():
        raise SourceMalformed(
            "check-run details_url must be a non-empty string for: {0}".format(
                _describe_command(argv)
            )
        )
    try:
        parsed = urllib.parse.urlsplit(details_url)
        port = parsed.port
    except ValueError as error:
        raise SourceMalformed(
            "check-run details_url is malformed for: {0}".format(_describe_command(argv))
        ) from error
    expected_prefix = "/{0}/actions/runs/".format(repository)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or (port is not None and port != 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(expected_prefix)
    ):
        raise SourceMalformed(
            "check-run details_url must identify one github actions workflow run for: {0}".format(
                _describe_command(argv)
            )
        )
    remainder = parsed.path[len(expected_prefix):]
    match = re.fullmatch(r"([0-9]+)/job/([0-9]+)", remainder)
    if match is None:
        raise SourceMalformed(
            "check-run details_url must carry an Actions run/job path for: {0}".format(
                _describe_command(argv)
            )
        )
    try:
        run_id = _require_positive_int(int(match.group(1)), "workflow run id", argv)
        _require_positive_int(int(match.group(2)), "workflow job id", argv)
    except (OverflowError, ValueError) as error:
        raise SourceMalformed(
            "check-run details_url carries an unrepresentable Actions run/job id for: {0}".format(
                _describe_command(argv)
            )
        ) from error
    return run_id


def _workflow_path_matches(actual: object, configured: str) -> bool:
    if actual == configured:
        return True
    if not isinstance(actual, str) or not actual.startswith(configured + "@"):
        return False
    ref = actual[len(configured) + 1:]
    return bool(ref) and not any(char.isspace() or ord(char) < 32 for char in ref)


def _normalize_workflow_path(value: object, label: str) -> str:
    path = _require_non_empty_str(value, label, ())
    if "@" not in path:
        return path
    workflow_path, ref = path.split("@", 1)
    if not workflow_path or not ref or any(char.isspace() or ord(char) < 32 for char in ref):
        raise SourceMalformed("{0} is not a qualified workflow path".format(label))
    return workflow_path + "@" + ref


def _validate_current_attempt(
    current_workflow: Mapping,
    current_attempt: Mapping,
    run_id: int,
    run_attempt: int,
) -> None:
    if _require_positive_int(current_workflow.get("id"), "workflow run id", ()) != run_id:
        raise SourceMalformed("current workflow run identity mismatch")
    if _require_positive_int(current_attempt.get("id"), "workflow attempt run id", ()) != run_id:
        raise SourceMalformed("current workflow attempt run id mismatch")
    if _require_positive_int(current_attempt.get("run_attempt"), "workflow attempt number", ()) != run_attempt:
        raise SourceMalformed("current workflow attempt number mismatch")
    for key, label in (
        ("workflow_id", "workflow id"),
        ("check_suite_id", "workflow check-suite id"),
    ):
        if _require_positive_int(current_attempt.get(key), label, ()) != _require_positive_int(
            current_workflow.get(key), label, ()
        ):
            raise SourceMalformed("current workflow attempt {0} mismatch".format(key))
    if _require_commit(current_attempt.get("head_sha"), "workflow attempt head_sha", ()) != _require_commit(
        current_workflow.get("head_sha"), "workflow-run head_sha", ()
    ):
        raise SourceMalformed("current workflow attempt head mismatch")
    if _normalize_workflow_path(current_attempt.get("path"), "workflow attempt path") != _normalize_workflow_path(
        current_workflow.get("path"), "workflow path"
    ):
        raise SourceMalformed("current workflow attempt path mismatch")
    if _require_non_empty_str(current_attempt.get("event"), "workflow attempt event", ()) != _require_non_empty_str(
        current_workflow.get("event"), "workflow event", ()
    ):
        raise SourceMalformed("current workflow attempt event mismatch")


class GitHubSource:
    """Fail-closed reader for one pull request via ``gh pr view``.

    The requested repository and PR number are bound into every read: the
    returned facts carry the requested values and a PR that reports a different
    number, a malformed head OID, a draft/state mismatch shape, an unbound URL,
    or a malformed status check rollup raises a typed source error.
    """

    def __init__(
        self,
        repository,
        pr_number,
        runner: Optional[Runner] = None,
        timeout=None,
        artifact_reader: Optional[Callable[[int, str], Mapping]] = None,
        artifact_runner: Optional[ArtifactRunner] = None,
    ):
        if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
            raise SourceMalformed("repository must look like owner/repo: {0!r}".format(repository))
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
            raise SourceMalformed("pr_number must be a positive integer")
        self._repository = repository
        self._pr_number = pr_number
        self._runner = runner or default_runner
        self._timeout = timeout
        self._artifact_reader = artifact_reader
        self._artifact_runner = artifact_runner or default_artifact_runner

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def pr_number(self) -> int:
        return self._pr_number

    def read_pr(self) -> PullRequestFacts:
        argv = (
            "gh",
            "pr",
            "view",
            str(self._pr_number),
            "--repo",
            self._repository,
            "--json",
            PR_JSON_FIELDS,
        )
        _, stdout, _ = _invoke(argv, self._timeout, self._runner)
        data = _parse_json_object(stdout, argv, "gh pr view")
        missing = [key for key in _REQUIRED_PR_KEYS if key not in data]
        if missing:
            raise SourceMalformed(
                "gh pr view missing key(s): {0} for: {1}".format(", ".join(missing), _describe_command(argv))
            )
        number = data["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number != self._pr_number:
            raise SourceMalformed(
                "PR number {0!r} does not bind to requested {1} for: {2}".format(
                    number, self._pr_number, _describe_command(argv)
                )
            )
        head_branch = _require_non_empty_str(data["headRefName"], "headRefName", argv)
        base_ref_name = _require_non_empty_str(data["baseRefName"], "baseRefName", argv)
        head_oid = _require_commit(data["headRefOid"], "headRefOid", argv)
        is_draft = data["isDraft"]
        if not isinstance(is_draft, bool):
            raise SourceMalformed("isDraft must be a boolean for: {0}".format(_describe_command(argv)))
        state = data["state"]
        if not isinstance(state, str) or state not in PR_STATES:
            raise SourceMalformed(
                "unsupported PR state {0!r} for: {1}".format(state, _describe_command(argv))
            )
        merge_state_status = _require_non_empty_str(data["mergeStateStatus"], "mergeStateStatus", argv)
        mergeable_value = _require_non_empty_str(data["mergeable"], "mergeable", argv)
        try:
            mergeable = MergeableState(mergeable_value)
        except ValueError as error:
            raise SourceMalformed(
                "unsupported mergeable value {0!r} for: {1}".format(mergeable_value, _describe_command(argv))
            ) from error
        url = _require_pr_url(data["url"], self._repository, self._pr_number, argv)
        rollup = data["statusCheckRollup"]
        if not isinstance(rollup, list):
            raise SourceMalformed(
                "statusCheckRollup must be a JSON array for: {0}".format(_describe_command(argv))
            )
        body = data["body"]
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise SourceMalformed(
                "body must be a string or null for: {0}".format(_describe_command(argv))
            )
        potential_merge = data["potentialMergeCommit"]
        if not isinstance(potential_merge, Mapping):
            raise SourceMalformed(
                "potentialMergeCommit must be a non-null object for: {0}".format(
                    _describe_command(argv)
                )
            )
        potential_merge_oid = _require_commit(
            potential_merge.get("oid"), "potentialMergeCommit.oid", argv
        )
        checks = tuple(parse_check(node, argv) for node in rollup)
        return PullRequestFacts(
            repository=self._repository,
            number=number,
            head_branch=head_branch,
            base_ref_name=base_ref_name,
            head_oid=head_oid,
            is_draft=is_draft,
            state=state,
            merge_state_status=merge_state_status,
            mergeable=mergeable,
            url=url,
            checks=checks,
            body=body,
            potential_merge_commit_oid=potential_merge_oid,
        )

    def _api_json(self, endpoint: str, *, label: str = "gh api"):
        argv = ["gh", "api"]
        argv.append(endpoint)
        argv = tuple(argv)
        _, stdout, _ = _invoke(argv, self._timeout, self._runner)
        return _parse_json_object(stdout, argv, label)

    def read_candidate_tip_config(
        self,
        head_oid: str,
        path: str = ".ai/project-closure-v1.json",
    ) -> Mapping:
        """Read and bind the closure config to one exact candidate-tip blob."""
        head_oid = _require_commit(head_oid, "candidate tip", ())
        if path != ".ai/project-closure-v1.json":
            raise SourceMalformed("candidate-tip config path is fixed")
        encoded_path = urllib.parse.quote(path, safe="/")
        content_endpoint = (
            "repos/{0}/contents/{1}?ref={2}".format(
                self._repository, encoded_path, head_oid
            )
        )
        content = self._api_json(content_endpoint, label="GitHub Contents API")
        content_path = content.get("path")
        if content_path != path:
            raise SourceMalformed("candidate-tip Contents response path mismatch")
        content_sha = _require_commit(content.get("sha"), "candidate-tip blob sha", ())
        encoding = content.get("encoding")
        encoded = content.get("content")
        if encoding != "base64" or not isinstance(encoded, str) or not encoded.strip():
            raise SourceMalformed("candidate-tip Contents response lacks base64 content")
        try:
            decoded = base64.b64decode("".join(encoded.split()), validate=True)
            raw = strict_json_loads(decoded, "candidate-tip closure config")
        except (ValueError, StrictJsonError) as error:
            raise SourceMalformed("candidate-tip closure config is not valid JSON") from error
        if not isinstance(raw, Mapping):
            raise SourceMalformed("candidate-tip closure config must be a JSON object")

        tree_endpoint = "repos/{0}/git/trees/{1}?recursive=1".format(
            self._repository, head_oid
        )
        tree = self._api_json(tree_endpoint, label="GitHub git tree API")
        entries = tree.get("tree")
        if not isinstance(entries, list) or tree.get("truncated") is not False:
            raise SourceMalformed("candidate-tip git tree is missing or truncated")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, Mapping)
            and entry.get("path") == path
            and entry.get("type") == "blob"
        ]
        if len(matches) != 1:
            raise SourceMalformed("candidate-tip git tree does not identify exactly one config blob")
        tree_sha = _require_commit(matches[0].get("sha"), "candidate-tip tree blob sha", ())
        if tree_sha != content_sha:
            raise SourceMalformed("candidate-tip Contents/tree blob identity mismatch")
        return raw

    def _workflow_id(self, path: str) -> int:
        match = re.fullmatch(r"\.github/workflows/([^/]+)", path) if isinstance(path, str) else None
        if match is None:
            raise SourceMalformed(
                "configured workflow path must be .github/workflows/<filename>"
            )
        filename = match.group(1)
        if filename in (".", "..") or any(
            char in filename for char in ("\\", "\x00")
        ):
            raise SourceMalformed(
                "configured workflow path must be .github/workflows/<filename>"
            )
        endpoint = "repos/{0}/actions/workflows/{1}".format(
            self._repository, urllib.parse.quote(filename, safe="")
        )
        data = self._api_json(endpoint, label="GitHub workflow API")
        workflow_id = _require_positive_int(data.get("id"), "workflow id", ())
        if data.get("path") != path:
            raise SourceMalformed("configured workflow path does not match provider response")
        return workflow_id


    def _workflow_run(self, run_id: int, run_attempt: Optional[int] = None) -> Mapping:
        if run_attempt is None:
            endpoint = "repos/{0}/actions/runs/{1}".format(self._repository, run_id)
        else:
            endpoint = "repos/{0}/actions/runs/{1}/attempts/{2}".format(
                self._repository, run_id, run_attempt
            )
        data = self._api_json(endpoint, label="GitHub workflow-run API")
        if _require_positive_int(data.get("id"), "workflow run id", ()) != run_id:
            raise SourceMalformed("workflow-run identity mismatch")
        actual_attempt = _require_positive_int(data.get("run_attempt"), "workflow run attempt", ())
        if run_attempt is not None and actual_attempt != run_attempt:
            raise SourceMalformed("workflow-run attempt identity mismatch")
        _require_positive_int(data.get("workflow_id"), "workflow id", ())
        _require_non_empty_str(data.get("path"), "workflow path", ())
        _require_non_empty_str(data.get("event"), "workflow event", ())
        _require_commit(data.get("head_sha"), "workflow-run head_sha", ())
        _require_positive_int(data.get("check_suite_id"), "workflow check-suite id", ())
        return data

    def _check_run_candidate(
        self,
        raw: Mapping,
        *,
        head_oid: str,
        workflow_path: str,
        workflow_action: str,
        workflow_id: Optional[int] = None,
        central_names: Optional[Sequence[str]] = None,
        workflow_cache: Optional[dict] = None,
    ) -> CheckRunCandidate:
        name = raw.get("name") if isinstance(raw.get("name"), str) else "<malformed>"
        authoritative = True
        try:
            check_id = _require_positive_int(raw.get("id"), "check-run id", ())
            run_head = _require_commit(raw.get("head_sha"), "check-run head_sha", ())
            status = raw.get("status")
            if not isinstance(status, str):
                raise SourceMalformed("check-run status is missing")
            status = status.upper()
            if status not in CHECK_RUN_STATUSES:
                raise SourceMalformed("unsupported check-run status: {0!r}".format(status))
            conclusion = raw.get("conclusion")
            if conclusion is not None:
                if not isinstance(conclusion, str):
                    raise SourceMalformed("check-run conclusion is malformed")
                conclusion = conclusion.upper()
                if conclusion not in CHECK_CONCLUSIONS:
                    raise SourceMalformed(
                        "unsupported check-run conclusion: {0!r}".format(conclusion)
                    )
            if status == "COMPLETED" and conclusion is None:
                raise SourceMalformed("completed check-run lacks conclusion")
            started_at = raw.get("started_at")
            _timestamp_key(started_at)
            completed_at = raw.get("completed_at")
            if status == "COMPLETED":
                _timestamp_key(completed_at)
            elif completed_at is not None:
                _timestamp_key(completed_at)
            details_url = raw.get("details_url")
            run_id = _parse_actions_run_id(details_url, self._repository, ())
            app = raw.get("app")
            if not isinstance(app, Mapping) or app.get("slug") != "github-actions":
                raise SourceMalformed("candidate app slug is not github-actions")
            suite = raw.get("check_suite")
            if not isinstance(suite, Mapping):
                raise SourceMalformed("candidate check-suite identity is missing")
            suite_id = _require_positive_int(suite.get("id"), "check-suite id", ())
            cache = workflow_cache if workflow_cache is not None else {}
            workflow = cache.get(("run", run_id))
            if workflow is None:
                workflow = self._workflow_run(run_id)
                cache[("run", run_id)] = workflow
            base_workflow_id = _require_positive_int(workflow.get("workflow_id"), "workflow id", ())
            actual_workflow_path = _require_non_empty_str(workflow.get("path"), "workflow path", ())
            event = _require_non_empty_str(workflow.get("event"), "workflow event/action", ())
            workflow_head = _require_commit(workflow.get("head_sha"), "workflow-run head_sha", ())
            if run_head != head_oid or workflow_head != head_oid:
                raise SourceMalformed("workflow/check candidate head does not match PR head")
            run_attempt = _require_positive_int(workflow.get("run_attempt"), "run attempt", ())
            if run_attempt > MAX_RUN_ATTEMPTS:
                raise SourceMalformed(
                    "run attempt exceeds {0}".format(MAX_RUN_ATTEMPTS)
                )
            current_attempt = cache.get((run_id, run_attempt))
            if current_attempt is None:
                current_attempt = self._workflow_run(run_id, run_attempt)
                cache[(run_id, run_attempt)] = current_attempt
            current_suite_id = _require_positive_int(
                current_attempt.get("check_suite_id"),
                "current workflow check-suite id",
                (),
            )
            _validate_current_attempt(workflow, current_attempt, run_id, run_attempt)
            authoritative = current_suite_id == suite_id == _require_positive_int(
                workflow.get("check_suite_id"),
                "workflow check-suite id",
                (),
            )
            enforce_central = workflow_id is not None and (
                central_names is None or name in central_names
            )
            if enforce_central and base_workflow_id != workflow_id:
                raise SourceMalformed("workflow id mismatch")
            if enforce_central and not _workflow_path_matches(actual_workflow_path, workflow_path):
                raise SourceMalformed("workflow path mismatch")
            if enforce_central and event != workflow_action:
                raise SourceMalformed("workflow event/action mismatch")
            attempt_path = _require_non_empty_str(current_attempt.get("path"), "workflow attempt path", ())
            if current_attempt.get("event") != event:
                raise SourceMalformed("workflow attempt event mismatch")
            if enforce_central and not _workflow_path_matches(attempt_path, workflow_path):
                raise SourceMalformed("workflow attempt path mismatch")
            if enforce_central and current_attempt.get("event") != workflow_action:
                raise SourceMalformed("workflow attempt event/action mismatch")
            outcome = _classify_check("check_run", status, conclusion)
            return CheckRunCandidate(
                result=CheckResult(
                    "check_run",
                    name,
                    status,
                    conclusion,
                    outcome,
                    details_url,
                    check_run_id=check_id,
                    head_sha=run_head,
                    started_at=started_at,
                    completed_at=completed_at,
                    app_slug=app.get("slug"),
                    check_suite_id=suite_id,
                    workflow_run_id=run_id,
                    workflow_id=base_workflow_id,
                    workflow_path=actual_workflow_path,
                    workflow_action=event,
                    workflow_event=event,
                    run_attempt=run_attempt,
                ),
                authoritative=authoritative,
            )
        except (SourceMalformed, TypeError, ValueError) as error:
            return CheckRunCandidate(
                result=None,
                name=name,
                error=str(error),
                authoritative=authoritative,
            )

    def read_check_run_candidates(
        self,
        head_oid: str,
        *,
        workflow_path: str,
        workflow_action: str,
        required_names: Sequence[str] = (),
        workflow_id: Optional[int] = None,
        central_names: Optional[Sequence[str]] = None,
    ) -> Tuple[CheckRunCandidate, ...]:
        head_oid = _require_commit(head_oid, "PR head", ())
        endpoint = (
            "repos/{0}/commits/{1}/check-runs?filter=all".format(
                self._repository, head_oid
            )
        )
        names = set(required_names)
        if not all(isinstance(name, str) and name for name in names):
            raise SourceMalformed("required check names are malformed")
        workflow_cache = {}
        candidates = []
        for raw in _read_explicit_pages(self, endpoint, "check_runs", "GitHub check-runs API"):
            if names and raw.get("name") not in names:
                continue
            candidates.append(
                self._check_run_candidate(
                    raw,
                    head_oid=head_oid,
                    workflow_path=workflow_path,
                    workflow_action=workflow_action,
                    workflow_id=workflow_id,
                    central_names=central_names,
                    workflow_cache=workflow_cache,
                )
            )
        return tuple(candidates)

    def _read_run_artifacts(self, run_id: int) -> Tuple[Mapping, ...]:
        endpoint = "repos/{0}/actions/runs/{1}/artifacts".format(
            self._repository, run_id
        )
        return _read_explicit_pages(self, endpoint, "artifacts", "GitHub artifacts API")

    def _download_snapshot_record(
        self,
        artifact_id: int,
        artifact_name: str,
        *,
        expected_size: int,
        expected_digest: Optional[str] = None,
    ) -> Mapping:
        if self._artifact_reader is not None:
            record = self._artifact_reader(artifact_id, artifact_name)
            if isinstance(record, bytes):
                if len(record) > MAX_SNAPSHOT_ARTIFACT_BYTES:
                    raise SourceMalformed("snapshot artifact exceeds the byte limit")
                if expected_digest is not None:
                    actual = "sha256:" + hashlib.sha256(record).hexdigest()
                    if actual != expected_digest:
                        raise SourceMalformed("snapshot artifact digest mismatch")
                try:
                    record = strict_json_loads(record, "snapshot artifact")
                except StrictJsonError as error:
                    raise SourceMalformed("snapshot artifact contains malformed JSON") from error
            elif isinstance(record, str):
                try:
                    record = strict_json_loads(record, "snapshot artifact")
                except StrictJsonError as error:
                    raise SourceMalformed("snapshot artifact contains malformed JSON") from error
            elif expected_digest is not None:
                raise SourceMalformed("snapshot artifact digest cannot be validated")
            if not isinstance(record, Mapping):
                raise SourceMalformed("snapshot artifact must contain one JSON object")
            return record
        with tempfile.TemporaryDirectory(prefix="pr-closure-artifact-") as directory:
            output = Path(directory) / "snapshot.zip"
            argv = (
                "gh", "api",
                "repos/{0}/actions/artifacts/{1}/zip".format(self._repository, artifact_id),
            )
            try:
                with output.open("w+b") as output_stream:
                    result = self._artifact_runner(
                        argv, self._timeout, output_stream, MAX_SNAPSHOT_ARTIFACT_BYTES
                    )
            except (subprocess.TimeoutExpired, TimeoutError) as error:
                raise SourceUnavailable(
                    "source command timed out after {0!r}s: {1}".format(
                        self._timeout, _describe_command(argv)
                    )
                ) from error
            except OSError as error:
                raise SourceUnavailable(
                    "cannot run source command {0}: {1}".format(
                        _describe_command(argv), redact(str(error))
                    )
                ) from error
            if not isinstance(result, tuple) or len(result) != 2:
                raise SourceMalformed("artifact runner returned an unexpected shape")
            returncode, stderr = result
            if not isinstance(returncode, int) or isinstance(returncode, bool) or not isinstance(stderr, str):
                raise SourceMalformed("artifact runner returned malformed result")
            if returncode != 0:
                raise SourceUnavailable(
                    "source command {0} exited with status {1}; stderr: {2}".format(
                        _describe_command(argv), returncode, redact(stderr)
                    )
                )
            if output.is_symlink() or (output.exists() and not output.is_file()):
                raise SourceMalformed("snapshot archive is not a regular file")
            if output.is_file():
                archive_size = output.stat().st_size
                if archive_size > expected_size:
                    raise SourceMalformed("snapshot archive exceeds its validated metadata size")
                if archive_size > MAX_SNAPSHOT_ARTIFACT_BYTES:
                    raise SourceMalformed("snapshot archive exceeds the byte limit")
                if expected_digest is not None:
                    digest = "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()
                    if digest != expected_digest:
                        raise SourceMalformed("snapshot artifact digest mismatch")
                try:
                    with zipfile.ZipFile(output) as archive:
                        members = archive.infolist()
                        if len(members) != 1:
                            raise SourceMalformed("snapshot archive must contain exactly one file")
                        member = members[0]
                        member_name = member.filename
                        member_path = Path(member_name)
                        mode = (member.external_attr >> 16) & 0o170000
                        if (
                            (mode and not stat.S_ISREG(mode))
                            or member.is_dir()
                            or member_path.is_absolute()
                            or ".." in member_path.parts
                        ):
                            raise SourceMalformed("snapshot archive contains an unsafe path")
                        if member.file_size > MAX_SNAPSHOT_ARTIFACT_BYTES:
                            raise SourceMalformed("snapshot archive file exceeds the byte limit")
                        record = strict_json_loads(archive.read(member), "snapshot artifact")
                except (OSError, zipfile.BadZipFile, StrictJsonError) as error:
                    raise SourceMalformed("snapshot archive is malformed") from error
                if not isinstance(record, Mapping):
                    raise SourceMalformed("snapshot artifact must contain one JSON object")
                return record
            raise SourceMalformed("snapshot artifact download did not produce an archive")

    def _read_snapshot(self, run_id: int, run_attempt: int) -> LivePrSnapshot:
        name = "ci-pr-snapshot-{0}-{1}".format(run_id, run_attempt)
        artifacts = self._read_run_artifacts(run_id)
        matches = [
            artifact
            for artifact in artifacts
            if artifact.get("name") == name
        ]
        if len(matches) != 1:
            raise SourceMalformed(
                "snapshot artifact {0} must resolve exactly once".format(name)
            )
        artifact_id = _require_positive_int(matches[0].get("id"), "artifact id", ())
        if type(matches[0].get("expired")) is not bool or matches[0]["expired"] is not False:
            raise SourceMalformed(
                "snapshot artifact {0} must provide expired=false".format(name)
            )
        size_in_bytes = matches[0].get("size_in_bytes")
        if not isinstance(size_in_bytes, int) or isinstance(size_in_bytes, bool) or size_in_bytes < 0:
            raise SourceMalformed("snapshot artifact size is malformed")
        if size_in_bytes > MAX_SNAPSHOT_ARTIFACT_BYTES:
            raise SourceMalformed("snapshot artifact exceeds the byte limit")
        digest = matches[0].get("digest")
        if digest is not None and (
            not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise SourceMalformed("snapshot artifact digest is malformed")
        record = self._download_snapshot_record(
            artifact_id,
            name,
            expected_size=size_in_bytes,
            expected_digest=digest,
        )
        expected = {
            "pr_number",
            "head_sha",
            "base_ref_name",
            "potential_merge_commit_oid",
            "body_sha256",
            "is_draft",
            "event_name",
            "event_sha",
            "workflow_path",
            "workflow_id",
            "workflow_action",
            "run_id",
            "run_attempt",
        }
        if set(record) != expected:
            raise SourceMalformed("snapshot artifact has an unexpected JSON shape")
        if (
            not isinstance(record["pr_number"], int)
            or isinstance(record["pr_number"], bool)
            or record["pr_number"] < 1
        ):
            raise SourceMalformed("snapshot pr_number is malformed")
        head_sha = _require_commit(record["head_sha"], "snapshot head_sha", ())
        merge_sha = _require_commit(
            record["potential_merge_commit_oid"],
            "snapshot potential_merge_commit_oid",
            (),
        )
        event_sha = _require_commit(record["event_sha"], "snapshot event_sha", ())
        if (
            not isinstance(record["base_ref_name"], str)
            or not record["base_ref_name"].strip()
            or not isinstance(record["body_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", record["body_sha256"]) is None
            or not isinstance(record["is_draft"], bool)
            or record["event_name"] != "pull_request"
            or not isinstance(record["workflow_path"], str)
            or not record["workflow_path"].strip()
            or not isinstance(record["workflow_action"], str)
            or not record["workflow_action"].strip()
        ):
            raise SourceMalformed("snapshot artifact carries malformed identity fields")
        workflow_id = _require_positive_int(record["workflow_id"], "snapshot workflow id", ())
        snapshot_run_id = _require_positive_int(record["run_id"], "snapshot run id", ())
        snapshot_attempt = _require_positive_int(
            record["run_attempt"], "snapshot run attempt", ()
        )
        if snapshot_run_id != run_id or snapshot_attempt != run_attempt:
            raise SourceMalformed("snapshot artifact run identity mismatch")
        return LivePrSnapshot(
            pr_number=record["pr_number"],
            head_sha=head_sha,
            base_ref_name=record["base_ref_name"],
            potential_merge_commit_oid=merge_sha,
            body_sha256=record["body_sha256"],
            is_draft=record["is_draft"],
            event_name=record["event_name"],
            event_sha=event_sha,
            workflow_path=record["workflow_path"],
            workflow_id=workflow_id,
            workflow_action=record["workflow_action"],
            run_id=snapshot_run_id,
            run_attempt=snapshot_attempt,
        )

    def read_live_ci(
        self,
        pr: PullRequestFacts,
        *,
        required_checks: Sequence[str],
        live_checks: Sequence[str],
        workflow_path: str,
        workflow_action: str,
    ) -> CiFacts:
        central_workflow_id = self._workflow_id(workflow_path) if live_checks else None
        candidates = self.read_check_run_candidates(
            pr.head_oid,
            workflow_path=workflow_path,
            workflow_action=workflow_action,
            required_names=required_checks,
            workflow_id=central_workflow_id,
            central_names=live_checks,
        )
        facts = select_check_run_candidates(
            candidates,
            required_checks,
            head_oid=pr.head_oid,
        )
        selected = {check.name: check for check in facts.checks}
        reasons = list(facts.reasons)
        snapshots = []
        for name in tuple(live_checks):
            check = selected.get(name)
            if check is None:
                reasons.append("live PR check {0} has no selected run".format(name))
                continue
            if (
                central_workflow_id is None
                or check.workflow_id != central_workflow_id
                or not _workflow_path_matches(check.workflow_path, workflow_path)
                or check.workflow_action != workflow_action
            ):
                reasons.append("live PR check {0} has mismatched workflow provenance".format(name))
                continue
            try:
                snapshot = self._read_snapshot(check.workflow_run_id, check.run_attempt)
                if not snapshot.matches_live(
                    pr_number=pr.number,
                    head_sha=pr.head_oid,
                    base_ref_name=pr.base_ref_name,
                    potential_merge_commit_oid=pr.potential_merge_commit_oid,
                    body=pr.body,
                    is_draft=pr.is_draft,
                    workflow_path=workflow_path,
                    workflow_id=check.workflow_id,
                    workflow_action=workflow_action,
                    run_id=check.workflow_run_id,
                    run_attempt=check.run_attempt,
                ):
                    reasons.append("live PR snapshot does not match current PR state")
                else:
                    snapshots.append(snapshot)
            except (SourceMalformed, SourceUnavailable) as error:
                reasons.append("live PR snapshot is unverified: {0}".format(error))
        if any("snapshot" in reason or "live PR check" in reason for reason in reasons):
            state = CiState.UNKNOWN if facts.ci_state is CiState.PASSING else facts.ci_state
        else:
            state = facts.ci_state
        first_snapshot = snapshots[0] if snapshots else None
        evidence_check = selected.get(tuple(live_checks)[0]) if live_checks else None
        if evidence_check is None:
            evidence_check = facts.checks[0] if facts.checks else None
        return replace(
            facts,
            ci_state=state,
            check_run_id=evidence_check.check_run_id if evidence_check else None,
            check_suite_id=evidence_check.check_suite_id if evidence_check else None,
            head_sha=evidence_check.head_sha if evidence_check else None,
            started_at=evidence_check.started_at if evidence_check else None,
            completed_at=evidence_check.completed_at if evidence_check else None,
            app_slug=evidence_check.app_slug if evidence_check else None,
            workflow_path=evidence_check.workflow_path if evidence_check else None,
            workflow_id=evidence_check.workflow_id if evidence_check else None,
            workflow_action=evidence_check.workflow_action if evidence_check else None,
            workflow_event=evidence_check.workflow_event if evidence_check else None,
            workflow_run_id=evidence_check.workflow_run_id if evidence_check else None,
            run_attempt=evidence_check.run_attempt if evidence_check else None,
            reasons=tuple(reasons),
            base_ref_name=pr.base_ref_name,
            potential_merge_commit_oid=pr.potential_merge_commit_oid,
            event_sha=first_snapshot.event_sha if first_snapshot else None,
            snapshot_body_sha256=first_snapshot.body_sha256 if first_snapshot else None,
        )


# ---------------------------------------------------------------------------
# GitHub issue reading (follow-up verification)
# ---------------------------------------------------------------------------

ISSUE_JSON_FIELDS = "number,state,url"
_REQUIRED_ISSUE_KEYS = ("number", "state", "url")
# GitHub issues are OPEN or CLOSED; SCHEDULED is a documented scheduled state
# the closure gate accepts as evidence of a live follow-up.
ISSUE_STATES = frozenset({"OPEN", "CLOSED", "SCHEDULED"})


@dataclass(frozen=True)
class IssueFacts:
    repository: str
    number: int
    state: str
    url: str


class GitHubIssueSource:
    """Fail-closed reader for one GitHub issue via ``gh issue view``.

    The requested repository and issue number are bound into every read: an
    issue reporting a different number, an unknown state, or an unbound URL
    raises a typed source error. An API failure or malformed response is an
    error, never a favorable default.
    """

    def __init__(self, repository, issue_number, runner: Optional[Runner] = None, timeout=None):
        if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
            raise SourceMalformed("repository must look like owner/repo: {0!r}".format(repository))
        if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
            raise SourceMalformed("issue_number must be a positive integer")
        self._repository = repository
        self._issue_number = issue_number
        self._runner = runner or default_runner
        self._timeout = timeout

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def issue_number(self) -> int:
        return self._issue_number

    def read_issue(self) -> IssueFacts:
        argv = (
            "gh",
            "issue",
            "view",
            str(self._issue_number),
            "--repo",
            self._repository,
            "--json",
            ISSUE_JSON_FIELDS,
        )
        _, stdout, _ = _invoke(argv, self._timeout, self._runner)
        data = _parse_json_object(stdout, argv, "gh issue view")
        missing = [key for key in _REQUIRED_ISSUE_KEYS if key not in data]
        if missing:
            raise SourceMalformed(
                "gh issue view missing key(s): {0} for: {1}".format(
                    ", ".join(missing), _describe_command(argv)
                )
            )
        number = data["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number != self._issue_number:
            raise SourceMalformed(
                "issue number {0!r} does not bind to requested {1} for: {2}".format(
                    number, self._issue_number, _describe_command(argv)
                )
            )
        state = data["state"]
        if not isinstance(state, str) or state not in ISSUE_STATES:
            raise SourceMalformed(
                "unsupported issue state {0!r} for: {1}".format(state, _describe_command(argv))
            )
        url = _require_github_url(data["url"], self._repository, self._issue_number, "issues", argv)
        return IssueFacts(
            repository=self._repository,
            number=number,
            state=state,
            url=url,
        )


# ---------------------------------------------------------------------------
# Infrastructure-failure evidence and CI classification
# ---------------------------------------------------------------------------

INFRA_FAILURE_EVENT = "INFRA_FAILURE"
_REQUIRED_INFRA_KEYS = ("event_type", "commit", "failed_job", "test_steps_not_started", "evidence_path")


@dataclass(frozen=True)
class InfraEvidence:
    failed_job: str
    test_steps_not_started: Tuple[str, ...]
    commit: str
    evidence_path: str

    def __post_init__(self):
        object.__setattr__(self, "test_steps_not_started", tuple(self.test_steps_not_started))


def require_infra_event(event) -> InfraEvidence:
    """Validate a durable infrastructure-failure evidence event.

    The classification may be accepted only from a durable event that names the
    failed job and proves at least one code-test step never began. Anything less
    or contradictory raises ``SourceMalformed`` so the caller stays branch
    failure/unknown instead of guessing infrastructure.
    """
    if not isinstance(event, Mapping):
        raise SourceMalformed("infrastructure failure evidence must be a JSON object/event")
    missing = [key for key in _REQUIRED_INFRA_KEYS if key not in event]
    if missing:
        raise SourceMalformed(
            "infrastructure failure event missing key(s): {0}".format(", ".join(missing))
        )
    event_type = event["event_type"]
    if event_type != INFRA_FAILURE_EVENT:
        raise SourceMalformed(
            "infrastructure failure event_type must be {0!r}: {1!r}".format(
                INFRA_FAILURE_EVENT, event_type
            )
        )
    failed_job = event["failed_job"]
    if not isinstance(failed_job, str) or not failed_job.strip():
        raise SourceMalformed("infrastructure failure event must name the failed job")
    steps = event["test_steps_not_started"]
    if not isinstance(steps, list) or not all(
        isinstance(step, str) and step.strip() for step in steps
    ):
        raise SourceMalformed(
            "infrastructure failure event test_steps_not_started must be a list of step names"
        )
    commit = event["commit"]
    if not isinstance(commit, str) or _COMMIT_ID_RE.fullmatch(commit) is None:
        raise SourceMalformed(
            "infrastructure failure event commit must be a 40-character lowercase hex id"
        )
    evidence_path = event["evidence_path"]
    if not isinstance(evidence_path, str) or not evidence_path.strip():
        raise SourceMalformed("infrastructure failure event must carry a durable evidence_path")
    return InfraEvidence(
        failed_job=failed_job,
        test_steps_not_started=tuple(step.strip() for step in steps),
        commit=commit,
        evidence_path=evidence_path,
    )


def _classify_outcomes(checks, required: bool):
    """Apply the existing outcome precedence to one effective check set."""
    if not checks:
        return CiState.UNKNOWN, ("no status checks present",)
    outcomes = [check.outcome for check in checks]
    if any(outcome is CheckOutcome.FAILURE for outcome in outcomes):
        return (
            CiState.BRANCH_FAILURE,
            ("at least one required check failed",) if required else ("at least one check failed",),
        )
    if any(outcome is CheckOutcome.PENDING for outcome in outcomes):
        return (
            CiState.PENDING,
            ("required checks still pending",) if required else ("checks still pending",),
        )
    if any(outcome is not CheckOutcome.PASSING for outcome in outcomes):
        reason = (
            "not every required check is passing evidence"
            if required
            else "not every check is passing evidence"
        )
        return CiState.UNKNOWN, (reason,)
    return (
        CiState.PASSING,
        ("all required checks passing",) if required else ("all checks passing",),
    )


def classify_ci(checks, head_oid: str, infra_event=None, required_checks=()) -> CiFacts:
    """Classify one PR's status-check rollup, never via an aggregate gate.

    Every check outcome is explicit: a single failed check wins over any green
    checks, pending checks stay pending, and skipped/neutral checks are never
    counted as passing evidence. A failed rollup may be reclassified as
    ``INFRA_FAILURE`` only when a durable ``INFRA_FAILURE`` event on the PR head
    commit names the failed job and proves no code-test step began, and the
    effective rollup holds exactly one failing check with that job's name;
    anything else leaves the state at branch failure with an explicit reason.
    The commit CI is bound to is the PR head OID and returned verbatim so
    local/remote/head/CI differences stay visible.

    An empty ``required_checks`` keeps the strict all-rollup behavior above.
    When non-empty, only checks whose names exactly match the declared list are
    authoritative: each declared name must appear exactly once in the live
    rollup, and the existing outcome precedence applies to that selection.
    Missing or duplicated declared names fail closed as ``UNKNOWN``, and checks
    outside the declared list never decide the state.
    """
    checks = tuple(checks)
    for check in checks:
        if not isinstance(check, CheckResult):
            raise TypeError("checks must contain CheckResult values")
    head_oid = _require_commit(head_oid, "PR head", ())

    required = tuple(required_checks)
    effective = checks
    if required:
        by_name = {}
        duplicates = set()
        for check in checks:
            if check.name in by_name:
                if check.name in required:
                    duplicates.add(check.name)
            else:
                by_name[check.name] = check
        missing = [name for name in required if name not in by_name]
        if missing or duplicates:
            reasons = []
            if missing:
                reasons.append(
                    "required check(s) missing: {0}".format(", ".join(sorted(missing)))
                )
            if duplicates:
                reasons.append(
                    "required check(s) with duplicate live results: {0}".format(
                        ", ".join(sorted(duplicates))
                    )
                )
            base = CiState.UNKNOWN
            effective = ()
        else:
            effective = tuple(by_name[name] for name in required)
            base, reasons = _classify_outcomes(effective, required=True)
    else:
        base, reasons = _classify_outcomes(checks, required=False)

    infra_job = None
    if base is CiState.BRANCH_FAILURE and infra_event is not None:
        evidence = require_infra_event(infra_event)
        failed_checks = [check for check in effective if check.outcome is CheckOutcome.FAILURE]
        if evidence.commit != head_oid:
            reasons = (
                "infrastructure evidence commit {0} does not match PR head {1}; "
                "stays branch failure".format(evidence.commit, head_oid),
            )
        elif len(failed_checks) != 1:
            reasons = (
                "infrastructure evidence names one failed job but {0} checks failed; "
                "stays branch failure".format(len(failed_checks)),
            )
        elif failed_checks[0].name != evidence.failed_job:
            reasons = (
                "infrastructure evidence names failed job {0} but the failed check is "
                "{1}; stays branch failure".format(
                    evidence.failed_job, failed_checks[0].name
                ),
            )
        elif not evidence.test_steps_not_started:
            reasons = (
                "infrastructure evidence does not prove a code-test step never began; "
                "stays branch failure",
            )
        else:
            base = CiState.INFRA_FAILURE
            infra_job = evidence.failed_job
            reasons = ("infrastructure evidence names failed job {0}".format(evidence.failed_job),)
    return CiFacts(
        ci_state=base,
        checks=checks,
        ci_commit=head_oid,
        infra_job=infra_job,
        reasons=reasons,
    )
