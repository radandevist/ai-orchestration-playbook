"""Fail-closed PR closure gate command-line interface.

Exact process exits:

    0  requested read/check succeeded
    2  invalid CLI, config, or review input
    3  source unavailable or malformed
    4  requested transition denied
    5  verification/projection command failed
    6  heavy-job lease unavailable

JSON stdout stays machine-readable; all diagnostics go to stderr and are
redacted. No command pushes, merges, modifies a PR branch, or treats
Trello/projection state as authority.
"""
from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Tuple

from pr_closure.contract import (
    ConfigValidationError,
    ReviewValidationError,
    command_digest,
    command_sequence_digest,
    validate_project_config,
)
from pr_closure.lease import (
    ForbiddenLeaseRoot,
    HeavyJobLease,
    LeasePathEscape,
    LeaseUnavailable,
)
from pr_closure.model import (
    ClosureSnapshot,
    ClosureState,
    Disposition,
    Evidence,
)
from pr_closure.review import require_live_binding, validate_review
from pr_closure.sources import (
    INFRA_FAILURE_EVENT,
    GitHubIssueSource,
    GitHubSource,
    GitSource,
    SourceMalformed,
    SourceUnavailable,
    WorktreeResolver,
    classify_ci,
    redact,
)
from pr_closure.state import derive_state
from pr_closure.store import (
    COMMIT_EVENT,
    VERIFICATION_EVENT,
    EvidenceConflict,
    ForbiddenEvidencePath,
    MalformedEvidence,
    RunStore,
    StorePathEscape,
)

EXIT_OK = 0
EXIT_INVALID_INPUT = 2
EXIT_SOURCE_ERROR = 3
EXIT_TRANSITION_DENIED = 4
EXIT_COMMAND_FAILED = 5
EXIT_LEASE_UNAVAILABLE = 6

VERIFICATION_SCHEMA_VERSION = 1
PROJECTION_RESULT_SCHEMA_VERSION = 1
PROJECTION_ADAPTER_TIMEOUT = 30.0
PROJECTION_STDOUT_MAX_BYTES = 65536
PROJECTION_STDERR_MAX_BYTES = 4096
PROJECTION_MAX_CHANGES = 100
PROJECTION_CHANGE_SUMMARY_MAX = 200
PROJECTION_RESULT_ALLOWED_KEYS = ("schema_version", "applied", "changes")
PROJECTION_CHANGE_ALLOWED_KEYS = ("type", "summary")
PROJECTION_CHANGE_TYPES = frozenset(
    {"list_update", "card_update", "card_create", "card_move", "card_archive"}
)

# GitHub issue states that prove a filed follow-up is still live.
ACCEPTABLE_ISSUE_STATES = frozenset({"OPEN", "SCHEDULED"})

GATE_PHASES = (
    ("local_review_ready", "local_review_ready_commands"),
    ("closure_acceptance", "closure_acceptance_commands"),
)


class CliError(Exception):
    """Base class for CLI-level failures mapped to an exact exit code."""


class CliInputError(CliError):
    """Invalid CLI, config, or review input (exit 2)."""


class VerificationFailure(CliError):
    """A configured verification command failed (exit 5)."""


class ProjectionFailure(CliError):
    """A configured tracking-projection command failed (exit 5)."""


def _err(message) -> None:
    sys.stderr.write("pr-closure: " + redact(str(message)) + "\n")


def _fail(exit_code: int, message) -> int:
    _err(message)
    return exit_code


def _read_config(path: str) -> Mapping:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CliInputError("cannot read config {0}: {1}".format(path, error))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CliInputError("config {0} is not valid JSON: {1}".format(path, error))
    if not isinstance(data, dict):
        raise CliInputError("config {0} must be a JSON object".format(path))
    return data


def _run_shell_command(command: str, cwd: str) -> dict:
    """Run one configured shell command string in the resolved PR worktree.

    The command is always passed as exactly one ``sh -c`` argv element and
    never spliced into a larger string; CLI arguments never reach the shell.
    ``cwd`` is the exact Git-reported PR worktree path (C6D-F1), never the
    CLI process directory or the configured anchor.

    stdout and stderr go to ``DEVNULL`` (T6L-F8): configured commands are
    arbitrary and their output cannot be safely redacted, so it is never
    captured or emitted on any CLI stream or persisted anywhere. The returned
    run record carries timestamps and the exit status only; the raw command
    text is never persisted (C6D-F4). A subprocess argument ``ValueError``
    (for example an embedded NUL byte) is mapped to the typed CLI failure
    contract (T6L-F9).
    """
    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            ("sh", "-c", command),
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, ValueError) as error:
        raise VerificationFailure(
            "cannot run verification command in the PR worktree: {0}".format(
                redact(str(error))
            )
        ) from error
    ended = datetime.now(timezone.utc)
    return {
        "started_at": started.isoformat(timespec="microseconds"),
        "ended_at": ended.isoformat(timespec="microseconds"),
        "exit_status": proc.returncode,
    }


def _require_live_pr(github, config):
    """Require a live open PR whose base branch matches the configured default.

    Fails closed before any derivation or evidence write: a closed/merged PR
    is never a source of approval, and a PR whose base differs from the
    configured default branch is not the PR this project config tracks.
    """
    pr = github.read_pr()
    if pr.state != "OPEN":
        raise SourceMalformed(
            "pull request is not open (state {0!r}); refusing to use a closed or merged PR".format(
                pr.state
            )
        )
    if pr.base_ref_name != config.default_branch:
        raise SourceMalformed(
            "pull request base branch {0!r} does not match configured default_branch {1!r}".format(
                pr.base_ref_name, config.default_branch
            )
        )
    return pr


def _resolve_pr_sources(config, pr_number):
    """Resolve the live PR worktree and its GitSource.

    Reads GitHub first, requires the PR base branch to equal the configured
    default branch, then resolves exactly one non-bare worktree whose
    checked-out branch equals the live PR head branch. A GitSource over the
    resolved path is bound to the live head branch, and every later read
    re-verifies the checked-out branch remains equal.
    """
    github = GitHubSource(config.repository, pr_number)
    pr = _require_live_pr(github, config)
    resolver = WorktreeResolver(config.repo_path)
    record = resolver.resolve(pr.head_branch)
    git = GitSource(record.path, pr.head_branch)
    return github, pr, git


def _bind_verification_target(config, pr_number):
    """Bind the exact commit to verify: pushed local tip equals PR head.

    Returns ``(store, git, facts)``; the resolved ``GitFacts`` carries the
    Git-reported PR worktree path that every gate command must run in
    (C6D-F1) and the live binding that must be revalidated before evidence
    is committed.
    """
    store = RunStore(config.closure_state_dir, config.project, pr_number)
    _, pr, git = _resolve_pr_sources(config, pr_number)
    facts = git.facts()
    if facts.local_commit != facts.remote_commit:
        raise SourceMalformed(
            "local commit differs from the pushed remote commit; "
            "push before recording evidence at the exact commit"
        )
    if facts.local_commit != pr.head_oid:
        raise SourceMalformed(
            "local commit does not match the pull request head commit; "
            "refusing to record evidence for a different commit"
        )
    return store, git, facts


def _require_blank_free(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CliInputError("{0} must be a non-empty string".format(label))
    return value


# ---------------------------------------------------------------------------
# Snapshot assembly shared by status, check-transition, and sync
# ---------------------------------------------------------------------------


def _contradictions_of(snapshot) -> Tuple[str, ...]:
    out = []
    if (
        snapshot.local_commit is not None
        and snapshot.remote_commit is not None
        and snapshot.local_commit != snapshot.remote_commit
    ):
        out.append("unpushed commit")
    if (
        snapshot.local_commit is not None
        and snapshot.ci_commit is not None
        and snapshot.ci_commit != snapshot.local_commit
    ):
        out.append("CI commit mismatch")
    if (
        snapshot.local_commit is not None
        and snapshot.review_commit is not None
        and snapshot.review_commit != snapshot.local_commit
    ):
        out.append("reviewed commit mismatch")
    if (
        snapshot.local_commit is not None
        and snapshot.durable_tip is not None
        and snapshot.durable_tip != snapshot.local_commit
    ):
        out.append("durable tip mismatch")
    return tuple(out)


def _status_snapshot(config, pr_number):
    """Derive the live fail-closed state from Git/GitHub plus durable records.

    Strictly read-only: no commit, verification, review, or projection write
    is ever made here. Missing, malformed, or contradictory evidence raises a
    typed source error so the caller fails closed.
    """
    store = RunStore(config.closure_state_dir, config.project, pr_number)
    _github, pr, git = _resolve_pr_sources(config, pr_number)
    facts = git.facts()
    events = store.read_events()

    durable_tip = None
    last_progress_at = None
    for event in events:
        if event["event_type"] == COMMIT_EVENT:
            durable_tip = event["commit"]
            try:
                last_progress_at = datetime.fromisoformat(event["timestamp"])
            except ValueError as error:
                raise SourceMalformed(
                    "durable commit event carries an unparseable timestamp: {0}".format(error)
                )

    expected_commands = tuple(
        (phase, command_digest(command))
        for phase, attr in GATE_PHASES
        for command in getattr(config, attr)
    )
    local_verification = None
    verification = store.bound_verification(facts.local_commit, expected_commands)
    if verification is not None:
        local_verification = True
    elif any(
        event["event_type"] == VERIFICATION_EVENT and event["commit"] == facts.local_commit
        for event in events
    ):
        local_verification = False

    infra_events = [
        event
        for event in events
        if event["event_type"] == INFRA_FAILURE_EVENT and event["commit"] == pr.head_oid
    ]
    infra_event = None
    if infra_events:
        last = infra_events[-1]
        infra_event = {
            "event_type": INFRA_FAILURE_EVENT,
            "commit": last["commit"],
            "failed_job": last.get("failed_job"),
            "test_steps_not_started": last.get("test_steps_not_started"),
            "evidence_path": last["evidence_path"],
        }
    ci = classify_ci(pr.checks, pr.head_oid, infra_event=infra_event)

    review_verdict = None
    review_commit = None
    blocking_findings = set()
    follow_up_findings = set()
    follow_up_issue_numbers = set()
    verdicts = set()
    for _stem, data in store.bound_reviews(facts.local_commit):
        try:
            record = validate_review(data)
        except ReviewValidationError as error:
            raise SourceMalformed(
                "durable review record is invalid: {0}".format(error)
            )
        try:
            require_live_binding(
                record, config.repository, pr_number, pr.head_branch, pr.head_oid
            )
        except ReviewValidationError as error:
            raise SourceMalformed(
                "durable review record fails live PR binding: {0}".format(error)
            )
        verdicts.add(record.verdict)
        if review_commit is None:
            review_commit = record.reviewed_commit
        for finding in record.findings:
            if finding.disposition is Disposition.BLOCKS_PR:
                blocking_findings.add(finding.id)
            elif finding.disposition is Disposition.FOLLOW_UP_ISSUE:
                follow_up_findings.add(finding.id)
                if finding.follow_up_issue is not None:
                    follow_up_issue_numbers.add(finding.follow_up_issue)
    if len(verdicts) > 1:
        raise SourceMalformed("durable review records disagree on the verdict")
    if verdicts:
        review_verdict = verdicts.pop()

    follow_ups_complete = None
    if follow_up_issue_numbers:
        acceptable = True
        for number in sorted(follow_up_issue_numbers):
            issue = GitHubIssueSource(config.repository, number).read_issue()
            if issue.state not in ACCEPTABLE_ISSUE_STATES:
                acceptable = False
                break
        follow_ups_complete = acceptable

    available = {
        Evidence.WORKTREE,
        Evidence.LOCAL_COMMIT,
        Evidence.REMOTE_COMMIT,
        Evidence.CI,
        Evidence.CI_COMMIT,
        Evidence.PR,
        Evidence.HEAD_BRANCH,
        Evidence.BASE_BRANCH,
        Evidence.CHECKED_OUT_BRANCH,
    }
    if durable_tip is not None:
        available.add(Evidence.DURABLE_TIP)

    snapshot = ClosureSnapshot(
        evidence_available=frozenset(available),
        local_commit=facts.local_commit,
        remote_commit=facts.remote_commit,
        ci_commit=ci.ci_commit,
        review_commit=review_commit,
        verification_commit=facts.local_commit if local_verification is True else None,
        durable_tip=durable_tip,
        head_branch=pr.head_branch,
        base_branch=pr.base_ref_name,
        checked_out_branch=facts.checked_out_branch,
        pr_state=pr.state,
        pr_is_draft=pr.is_draft,
        worktree_clean=facts.worktree_clean,
        local_verification=local_verification,
        ci_state=ci.ci_state,
        review_owned=False,
        review_verdict=review_verdict,
        blocking_findings=tuple(sorted(blocking_findings)),
        follow_up_findings=tuple(sorted(follow_up_findings)),
        follow_ups_complete=follow_ups_complete,
        infra_retry_budget=config.infra_retry_budget,
        infra_retries_used=len(infra_events),
        stagnation_budget_minutes=config.stagnation_budget_minutes,
        last_progress_at=last_progress_at,
    )
    decision = derive_state(snapshot, datetime.now(timezone.utc))
    return snapshot, decision


def _yesno(value) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_status(config, args) -> int:
    snapshot, decision = _status_snapshot(config, args.pr)
    if args.json:
        payload = {
            "schema_version": 1,
            "project": config.project,
            "pr": args.pr,
            "state": decision.state.value,
            "reasons": list(decision.reasons),
            "allowed_actions": list(decision.allowed_actions),
            "commit": snapshot.local_commit,
            "ci_state": snapshot.ci_state.value,
            "worktree_clean": snapshot.worktree_clean,
            "local_verification": snapshot.local_verification,
            "durable_tip": snapshot.durable_tip,
            "review_verdict": (
                snapshot.review_verdict.value if snapshot.review_verdict is not None else None
            ),
        }
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            "\n".join(
                (
                    "project: {0}".format(config.project),
                    "pr: {0}".format(args.pr),
                    "state: {0}".format(decision.state.value),
                    "reasons: {0}".format("; ".join(decision.reasons)),
                    "allowed_actions: {0}".format(", ".join(decision.allowed_actions)),
                    "commit: {0}".format(snapshot.local_commit or "(none)"),
                    "ci_state: {0}".format(snapshot.ci_state.value),
                    "worktree_clean: {0}".format(_yesno(snapshot.worktree_clean)),
                    "local_verification: {0}".format(_yesno(snapshot.local_verification)),
                    "durable_tip: {0}".format(snapshot.durable_tip or "(none)"),
                    "review_verdict: {0}".format(
                        snapshot.review_verdict.value if snapshot.review_verdict else "(none)"
                    ),
                )
            )
            + "\n"
        )
    return EXIT_OK


def cmd_import_review(config, args) -> int:
    store = RunStore(config.closure_state_dir, config.project, args.pr)
    github = GitHubSource(config.repository, args.pr)
    pr = _require_live_pr(github, config)
    try:
        raw = Path(args.review).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CliInputError(
            "cannot read review artifact {0}: {1}".format(args.review, error)
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CliInputError(
            "review artifact {0} is not valid JSON: {1}".format(args.review, error)
        )
    if not isinstance(data, dict):
        raise CliInputError("review artifact must be a JSON object")
    record = validate_review(data)
    require_live_binding(
        record, config.repository, args.pr, pr.head_branch, pr.head_oid
    )
    durable_tip = store.current_commit()
    if durable_tip is None or durable_tip != record.reviewed_commit:
        raise CliInputError(
            "review does not bind to the durably recorded tip; run "
            "record-verification at the exact commit first"
        )
    review_id = Path(args.review).stem
    store.write_review(record.reviewed_commit, review_id, data)
    return EXIT_OK


def cmd_record_verification(config, args) -> int:
    commands = tuple(
        (phase, command)
        for phase, attr in GATE_PHASES
        for command in getattr(config, attr)
    )
    expected_commands = tuple(
        (phase, command_digest(command)) for phase, command in commands
    )
    config_digest = command_sequence_digest(expected_commands)
    store, git, facts = _bind_verification_target(config, args.pr)
    commit = facts.local_commit
    worktree = facts.worktree_path
    lease_argv = (
        "pr-closure",
        "record-verification",
        command_digest(" && ".join(command for _, command in commands)),
    )
    lease = HeavyJobLease(config.closure_state_dir, config.project, args.pr, lease_argv)
    lease.acquire()
    with lease:
        store.record_commit(commit, config.repo_path)
        runs = []
        failed = None
        for index, (phase, command) in enumerate(commands, start=1):
            run = _run_shell_command(command, cwd=worktree)
            run["phase"] = phase
            run["command_digest"] = command_digest(command)
            run["command_label"] = "{0}:{1}".format(phase, index)
            runs.append(run)
            if run["exit_status"] != 0:
                failed = run
                break
        if failed is None:
            rechecked = git.facts()
            if (
                rechecked.local_commit != commit
                or rechecked.worktree_path != worktree
            ):
                raise SourceMalformed(
                    "verification worktree binding changed while the gate ran; "
                    "refusing to commit evidence for a moved or re-tipped PR worktree"
                )
            record = {
                "schema_version": VERIFICATION_SCHEMA_VERSION,
                "project": config.project,
                "pr": args.pr,
                "commit": commit,
                "config_digest": config_digest,
                "outcome": "PASSED",
                "started_at": runs[0]["started_at"],
                "ended_at": runs[-1]["ended_at"],
                "commands": runs,
            }
            store.write_verification(commit, config_digest, record)
            return EXIT_OK
        store.append_event(
            VERIFICATION_EVENT,
            commit,
            str(store.events_path),
            payload={
                "outcome": "FAILED",
                "started_at": runs[0]["started_at"],
                "ended_at": runs[-1]["ended_at"],
                "commands": [dict(run) for run in runs],
                "failed_command_digest": failed["command_digest"],
            },
        )
        raise VerificationFailure(
            "verification command failed with exit status {0} ({1})".format(
                failed["exit_status"], failed["command_label"]
            )
        )


def cmd_record_infra_failure(config, args) -> int:
    failed_job = _require_blank_free(args.failed_job, "failed-job")
    steps = tuple(_require_blank_free(step, "test-steps-not-started") for step in args.test_steps_not_started)
    store, _git, facts = _bind_verification_target(config, args.pr)
    commit = facts.local_commit
    store.record_commit(commit, config.repo_path)
    store.append_event(
        INFRA_FAILURE_EVENT,
        commit,
        str(store.events_path),
        payload={
            "failed_job": failed_job,
            "test_steps_not_started": list(steps),
        },
    )
    return EXIT_OK


def cmd_check_transition(config, args) -> int:
    _, decision = _status_snapshot(config, args.pr)
    try:
        target = ClosureState(args.to)
    except ValueError:
        raise CliInputError(
            "--to must be an exact closure state, got {0!r}".format(args.to)
        )
    allowed = decision.state == target
    sys.stdout.write(
        "state={0} target={1} allowed={2}\n".format(
            decision.state.value, target.value, "yes" if allowed else "no"
        )
    )
    if not allowed:
        _err(
            "transition denied: current state {0} is not the requested target {1}".format(
                decision.state.value, target.value
            )
        )
        return EXIT_TRANSITION_DENIED
    return EXIT_OK


def _require_projection_adapter(path):
    """Require an absolute, non-symlink, regular, executable adapter path."""
    if not isinstance(path, str) or not path.strip():
        raise CliInputError("--projection-adapter must be a non-empty path")
    if not os.path.isabs(path):
        raise CliInputError("--projection-adapter must be an absolute path")
    try:
        entry = os.lstat(path)
    except OSError as error:
        raise CliInputError("cannot stat projection adapter {0}: {1}".format(path, error))
    if stat.S_ISLNK(entry.st_mode):
        raise CliInputError("--projection-adapter must not be a symlink")
    if not stat.S_ISREG(entry.st_mode):
        raise CliInputError("--projection-adapter must be a regular file")
    if not os.access(path, os.X_OK):
        raise CliInputError("--projection-adapter must be executable")
    return path


def _terminate_process_group(proc) -> None:
    """Kill the adapter's entire process group, with a per-platform fallback.

    The adapter starts as a session leader (``start_new_session=True``), so
    every descendant that does not create its own session shares the group;
    killing the group closes inherited pipe ends that would otherwise hang a
    bounded read loop. Platforms without process groups fall back to killing
    only the direct child.
    """
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _reap_process(proc) -> None:
    """Reap the direct child with a bound, so cleanup never hangs the CLI."""
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, TimeoutError):
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, TimeoutError):
            pass


def _invoke_adapter(argv, timeout):
    """Invoke the projection adapter as an argv list, never through a shell.

    stdout and stderr are captured as raw bytes through one deadline-bounded
    selector loop (T6LC-F7): a single deadline covers parent execution and
    pipe EOF, the 65,536-byte stdout and 4,096-byte stderr limits are
    enforced while reading, and the entire process group is terminated and
    reaped on timeout, overflow, or read failure. The bounded stdout is
    decoded strictly as UTF-8 only after capture; invalid UTF-8, overflow,
    timeout, and read errors are typed :class:`ProjectionFailure` values with
    fixed safe messages. Adapter stderr and adapter-controlled text are never
    echoed on any CLI stream.
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        raise ProjectionFailure(
            "cannot run projection adapter: {0}".format(redact(str(error)))
        ) from error

    deadline = time.monotonic() + timeout
    streams = {
        proc.stdout: {
            "limit": PROJECTION_STDOUT_MAX_BYTES,
            "buf": bytearray(),
        },
        proc.stderr: {
            "limit": PROJECTION_STDERR_MAX_BYTES,
            "buf": bytearray(),
        },
    }
    reason = None
    try:
        with selectors.DefaultSelector() as selector:
            for stream in streams:
                selector.register(stream, selectors.EVENT_READ, stream)
            closed = set()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "timeout"
                    break
                ready = selector.select(timeout=min(remaining, 0.25))
                for key, _ in ready:
                    stream = key.fileobj
                    try:
                        chunk = stream.read1(65536)
                    except OSError:
                        reason = "read"
                        break
                    if not chunk:
                        closed.add(stream)
                        selector.unregister(stream)
                        continue
                    entry = streams[stream]
                    entry["buf"].extend(chunk)
                    if len(entry["buf"]) > entry["limit"]:
                        reason = "overflow"
                        break
                if reason is not None:
                    break
                if proc.poll() is not None and closed == set(streams):
                    break
    except OSError:
        reason = "read"
    finally:
        if reason is not None or proc.poll() is None:
            _terminate_process_group(proc)
            _reap_process(proc)
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass

    if reason == "timeout":
        raise ProjectionFailure(
            "projection adapter timed out after {0}s".format(timeout)
        )
    if reason == "overflow":
        raise ProjectionFailure(
            "projection adapter output exceeded the bounded stream limit"
        )
    if reason == "read":
        raise ProjectionFailure("projection adapter stream read failed")
    if proc.returncode != 0:
        raise ProjectionFailure(
            "projection adapter exited with status {0}".format(proc.returncode)
        )
    try:
        return bytes(streams[proc.stdout]["buf"]).decode("utf-8")
    except UnicodeDecodeError:
        raise ProjectionFailure("projection adapter returned invalid UTF-8 output")


def _parse_adapter_result(stdout, mode):
    """Validate the versioned JSON result of a projection adapter run.

    The version-1 contract is strict and bounded (T6L-F7): unknown result
    keys are rejected, and ``changes`` must be an array of exactly
    ``{type, summary}`` objects whose type is a documented projection change
    type, whose summary is a bounded non-empty string, and whose count does
    not exceed ``PROJECTION_MAX_CHANGES``. Arbitrary scalars or opaque
    objects are never accepted.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise ProjectionFailure("projection adapter returned malformed JSON")
    if not isinstance(data, dict):
        raise ProjectionFailure("projection adapter must return a JSON object")
    unknown = sorted(key for key in data if key not in PROJECTION_RESULT_ALLOWED_KEYS)
    if unknown:
        raise ProjectionFailure("projection adapter result carries unknown key(s)")
    schema_version = data.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != PROJECTION_RESULT_SCHEMA_VERSION
    ):
        raise ProjectionFailure(
            "projection adapter result must declare schema_version {0}".format(
                PROJECTION_RESULT_SCHEMA_VERSION
            )
        )
    applied = data.get("applied")
    if not isinstance(applied, bool):
        raise ProjectionFailure("projection adapter result must declare an applied boolean")
    changes = data.get("changes")
    if not isinstance(changes, list):
        raise ProjectionFailure("projection adapter result must declare a changes array")
    if len(changes) > PROJECTION_MAX_CHANGES:
        raise ProjectionFailure(
            "projection adapter result changes exceed the bound of {0}".format(
                PROJECTION_MAX_CHANGES
            )
        )
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ProjectionFailure(
                "projection change at index {0} must be an object".format(index)
            )
        unknown_change = sorted(
            key for key in change if key not in PROJECTION_CHANGE_ALLOWED_KEYS
        )
        if unknown_change:
            raise ProjectionFailure(
                "projection change at index {0} carries unknown key(s)".format(index)
            )
        change_type = change.get("type")
        if change_type not in PROJECTION_CHANGE_TYPES:
            raise ProjectionFailure(
                "projection change at index {0} has unknown type".format(index)
            )
        summary = change.get("summary")
        if not isinstance(summary, str) or not (1 <= len(summary) <= PROJECTION_CHANGE_SUMMARY_MAX):
            raise ProjectionFailure(
                "projection change at index {0} summary must be a string of 1..{1} chars".format(
                    index, PROJECTION_CHANGE_SUMMARY_MAX
                )
            )
    if mode == "dry-run" and applied:
        raise ProjectionFailure("dry-run must report applied: false")
    if mode == "apply" and not applied:
        raise ProjectionFailure("apply must report applied: true")
    return data


def cmd_sync(config, args) -> int:
    snapshot, decision = _status_snapshot(config, args.pr)
    contradictions = _contradictions_of(snapshot)
    if contradictions:
        raise SourceMalformed(
            "contradictory source evidence: {0}; no projection writes performed".format(
                "; ".join(contradictions)
            )
        )
    projection = config.tracking_projection
    if projection is None:
        if args.apply:
            raise CliInputError(
                "sync --apply requires a configured tracking_projection"
            )
        sys.stdout.write(
            "state={0}\n(no tracking_projection configured; --apply would be refused)\n".format(
                decision.state.value
            )
        )
        return EXIT_OK
    if args.projection_adapter is None:
        raise CliInputError(
            "sync requires --projection-adapter to project mapping {0!r}".format(projection)
        )
    adapter = _require_projection_adapter(args.projection_adapter)
    mode = "apply" if args.apply else "dry-run"
    argv = (
        adapter,
        "--project", config.project,
        "--pr", str(args.pr),
        "--mapping", projection,
        "--state", decision.state.value,
        "--mode", mode,
    )
    stdout = _invoke_adapter(argv, PROJECTION_ADAPTER_TIMEOUT)
    result = _parse_adapter_result(stdout, mode)
    sys.stdout.write(
        "state={0}\nprojection {1}: changes={2}\n".format(
            decision.state.value,
            "applied" if mode == "apply" else "dry-run",
            len(result["changes"]),
        )
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parsing and dispatch
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-closure",
        description="Fail-closed PR closure gate: derive state, record evidence, "
        "check transitions, and sync a tracking projection.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    status = sub.add_parser("status", help="derive live state (strictly read-only)")
    status.add_argument("--config", required=True, metavar="FILE", help="project closure config")
    status.add_argument("--pr", required=True, type=int, metavar="N", help="pull request number")
    status.add_argument("--json", action="store_true", help="emit machine-readable JSON on stdout")
    status.set_defaults(func=cmd_status)

    import_review = sub.add_parser("import-review", help="validate and store a review artifact")
    import_review.add_argument("--config", required=True, metavar="FILE")
    import_review.add_argument("--pr", required=True, type=int, metavar="N")
    import_review.add_argument("--review", required=True, metavar="FILE", help="JSON review artifact")
    import_review.set_defaults(func=cmd_import_review)

    record_verification = sub.add_parser(
        "record-verification", help="run the local review-ready gate under the heavy-job lease"
    )
    record_verification.add_argument("--config", required=True, metavar="FILE")
    record_verification.add_argument("--pr", required=True, type=int, metavar="N")
    record_verification.set_defaults(func=cmd_record_verification)

    record_infra = sub.add_parser(
        "record-infra-failure", help="store bounded commit-bound infrastructure failure evidence"
    )
    record_infra.add_argument("--config", required=True, metavar="FILE")
    record_infra.add_argument("--pr", required=True, type=int, metavar="N")
    record_infra.add_argument("--failed-job", required=True, metavar="NAME")
    record_infra.add_argument(
        "--test-steps-not-started", required=True, nargs="+", metavar="STEP"
    )
    record_infra.set_defaults(func=cmd_record_infra_failure)

    check_transition = sub.add_parser(
        "check-transition", help="derive state and require an exact closure state target"
    )
    check_transition.add_argument("--config", required=True, metavar="FILE")
    check_transition.add_argument("--pr", required=True, type=int, metavar="N")
    check_transition.add_argument(
        "--to", required=True, metavar="STATE", help="exact ClosureState value (e.g. APPROVED)"
    )
    check_transition.set_defaults(func=cmd_check_transition)

    sync = sub.add_parser("sync", help="plan or apply tracking-projection changes")
    sync.add_argument("--config", required=True, metavar="FILE")
    sync.add_argument("--pr", required=True, type=int, metavar="N")
    sync.add_argument(
        "--apply", action="store_true", help="apply the projection through the adapter"
    )
    sync.add_argument(
        "--projection-adapter",
        metavar="EXECUTABLE",
        help="absolute non-symlink executable implementing the projection protocol",
    )
    sync.set_defaults(func=cmd_sync)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = validate_project_config(_read_config(args.config))
        return args.func(config, args)
    except CliInputError as error:
        return _fail(EXIT_INVALID_INPUT, error)
    except ReviewValidationError as error:
        return _fail(EXIT_INVALID_INPUT, error)
    except ConfigValidationError as error:
        return _fail(EXIT_INVALID_INPUT, error)
    except EvidenceConflict as error:
        return _fail(EXIT_INVALID_INPUT, error)
    except (StorePathEscape, ForbiddenEvidencePath) as error:
        return _fail(EXIT_INVALID_INPUT, error)
    except (ForbiddenLeaseRoot, LeasePathEscape) as error:
        return _fail(EXIT_INVALID_INPUT, error)
    except (SourceUnavailable, SourceMalformed) as error:
        return _fail(EXIT_SOURCE_ERROR, error)
    except MalformedEvidence as error:
        return _fail(EXIT_SOURCE_ERROR, error)
    except LeaseUnavailable as error:
        return _fail(EXIT_LEASE_UNAVAILABLE, error)
    except VerificationFailure as error:
        return _fail(EXIT_COMMAND_FAILED, error)
    except ProjectionFailure as error:
        return _fail(EXIT_COMMAND_FAILED, error)
    except ValueError as error:
        return _fail(EXIT_INVALID_INPUT, error)


if __name__ == "__main__":
    sys.exit(main())
