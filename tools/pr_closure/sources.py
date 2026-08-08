from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Mapping, Optional, Sequence, Tuple

from pr_closure.contract import COMMIT_ID_PATTERN, REPOSITORY_PATTERN
from pr_closure.model import CiState

_COMMIT_ID_RE = re.compile(COMMIT_ID_PATTERN)
_REPOSITORY_RE = re.compile(REPOSITORY_PATTERN)
_NON_BLANK_RE = re.compile(r"\S")
_WHITESPACE_RE = re.compile(r"\s")

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

_CREDENTIALS_IN_URL_RE = re.compile(r"(?i)(?P<scheme>https?://)[^/@\s]+@")
_SECRET_KV_RE = re.compile(r"(?i)(password|token|secret|api[_-]?key|access[_-]?token|auth[a-z_-]*)=([^&\s]+)")


def _redact(text: str) -> str:
    text = _CREDENTIALS_IN_URL_RE.sub(r"\g<scheme>***@", text)
    text = _SECRET_KV_RE.sub(lambda match: match.group(1) + "=***", text)
    return text


def _describe_command(argv: Sequence[str]) -> str:
    return _redact(" ".join(str(part) for part in argv))


# ---------------------------------------------------------------------------
# Command runner
# ---------------------------------------------------------------------------

CommandResult = Tuple[int, str, str]
Runner = Callable[[Sequence[str], Optional[float]], CommandResult]


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
                _describe_command(argv), _redact(str(error))
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
                _describe_command(argv), detail, _redact(stderr)
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
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SourceMalformed(
            "{0} returned malformed JSON for: {1}: {2}".format(label, _describe_command(argv), error)
        ) from error
    if not isinstance(parsed, dict):
        raise SourceMalformed("{0} must be a JSON object for: {1}".format(label, _describe_command(argv)))
    return parsed


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
        bare, path, rest = False, marker[len("worktree "):], lines[1:]
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
        elif key in ("bare", "prunable", "reftable", "locked", "lock_reason"):
            continue
        else:
            raise SourceMalformed(
                "unknown worktree field {0!r} for: {1}".format(key, _describe_command(argv))
            )
    if not bare and head is None:
        raise SourceMalformed(
            "worktree record missing HEAD for: {0}".format(_describe_command(argv))
        )
    resolved = None
    if path is not None:
        resolved = os.path.realpath(os.path.abspath(path))
        head = _require_commit(head, "worktree HEAD", argv)
    return WorktreeRecord(path=resolved, head=head, branch=branch, detached=detached, bare=bare)


# ---------------------------------------------------------------------------
# GitSource
# ---------------------------------------------------------------------------

WORKTREE_LIST_COMMAND = ("git", "worktree", "list", "--porcelain")
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
        argv = WORKTREE_LIST_COMMAND
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


# ---------------------------------------------------------------------------
# GitHub PR reading
# ---------------------------------------------------------------------------

PR_JSON_FIELDS = "number,headRefName,headRefOid,isDraft,state,statusCheckRollup,url"
_REQUIRED_PR_KEYS = ("number", "headRefName", "headRefOid", "isDraft", "state", "statusCheckRollup", "url")
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


@dataclass(frozen=True)
class PullRequestFacts:
    repository: str
    number: int
    head_branch: str
    head_oid: str
    is_draft: bool
    state: str
    url: str
    checks: Tuple[CheckResult, ...]

    def __post_init__(self):
        object.__setattr__(self, "checks", tuple(self.checks))


@dataclass(frozen=True)
class CiFacts:
    ci_state: CiState
    checks: Tuple[CheckResult, ...]
    ci_commit: str
    infra_job: Optional[str] = None
    reasons: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "reasons", tuple(self.reasons))


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


def _require_pr_url(value, repository: str, number: int, argv) -> str:
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
    expected = "/{0}/pull/{1}".format(repository, number)
    if parsed.path not in (expected, expected + "/"):
        raise SourceMalformed(
            "PR url must bind exactly to {0} pull request {1} for: {2}".format(
                repository, number, _describe_command(argv)
            )
        )
    return value


class GitHubSource:
    """Fail-closed reader for one pull request via ``gh pr view``.

    The requested repository and PR number are bound into every read: the
    returned facts carry the requested values and a PR that reports a different
    number, a malformed head OID, a draft/state mismatch shape, an unbound URL,
    or a malformed status check rollup raises a typed source error.
    """

    def __init__(self, repository, pr_number, runner: Optional[Runner] = None, timeout=None):
        if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
            raise SourceMalformed("repository must look like owner/repo: {0!r}".format(repository))
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
            raise SourceMalformed("pr_number must be a positive integer")
        self._repository = repository
        self._pr_number = pr_number
        self._runner = runner or default_runner
        self._timeout = timeout

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
        head_oid = _require_commit(data["headRefOid"], "headRefOid", argv)
        is_draft = data["isDraft"]
        if not isinstance(is_draft, bool):
            raise SourceMalformed("isDraft must be a boolean for: {0}".format(_describe_command(argv)))
        state = data["state"]
        if not isinstance(state, str) or state not in PR_STATES:
            raise SourceMalformed(
                "unsupported PR state {0!r} for: {1}".format(state, _describe_command(argv))
            )
        url = _require_pr_url(data["url"], self._repository, self._pr_number, argv)
        rollup = data["statusCheckRollup"]
        if not isinstance(rollup, list):
            raise SourceMalformed(
                "statusCheckRollup must be a JSON array for: {0}".format(_describe_command(argv))
            )
        checks = tuple(parse_check(node, argv) for node in rollup)
        return PullRequestFacts(
            repository=self._repository,
            number=number,
            head_branch=head_branch,
            head_oid=head_oid,
            is_draft=is_draft,
            state=state,
            url=url,
            checks=checks,
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


def classify_ci(checks, head_oid: str, infra_event=None) -> CiFacts:
    """Classify one PR's complete status-check rollup, never via an aggregate gate.

    Every check outcome is explicit: a single failed check wins over any green
    checks, pending checks stay pending, and skipped/neutral checks are never
    counted as passing evidence. A failed rollup may be reclassified as
    ``INFRA_FAILURE`` only when a durable ``INFRA_FAILURE`` event on the PR head
    commit names the failed job and proves no code-test step began, and the
    rollup holds exactly one failing check with that job's name; anything else
    leaves the state at branch failure with an explicit reason. The commit CI
    is bound to is the PR head OID and returned verbatim so local/remote/head/CI
    differences stay visible.
    """
    checks = tuple(checks)
    for check in checks:
        if not isinstance(check, CheckResult):
            raise TypeError("checks must contain CheckResult values")
    head_oid = _require_commit(head_oid, "PR head", ())

    if not checks:
        base, reasons = CiState.UNKNOWN, ("no status checks present",)
    else:
        outcomes = [check.outcome for check in checks]
        if any(outcome is CheckOutcome.FAILURE for outcome in outcomes):
            base, reasons = CiState.BRANCH_FAILURE, ("at least one check failed",)
        elif any(outcome is CheckOutcome.PENDING for outcome in outcomes):
            base, reasons = CiState.PENDING, ("checks still pending",)
        elif any(outcome is not CheckOutcome.PASSING for outcome in outcomes):
            base, reasons = CiState.UNKNOWN, ("not every check is passing evidence",)
        else:
            base, reasons = CiState.PASSING, ("all checks passing",)

    infra_job = None
    if base is CiState.BRANCH_FAILURE and infra_event is not None:
        evidence = require_infra_event(infra_event)
        failed_checks = [
            check for check in checks if check.outcome is CheckOutcome.FAILURE
        ]
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
