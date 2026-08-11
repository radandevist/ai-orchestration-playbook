import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pr_closure.cli import ProjectionFailure, _invoke_adapter
from pr_closure.lease import HeavyJobLease
from pr_closure.store import RunStore

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
PROJECT = "publyapp"
REPOSITORY = "owner/repo"
BRANCH = "feature/close"
BASE_BRANCH = "develop"
PR = 42

PR_CLOSURE = Path(__file__).resolve().parent.parent / "pr-closure"
DURABLE_TMP = "/var/tmp"

PASSING_ROLLUP = [
    {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
]

BASE_CONFIG = {
    "schema_version": 1,
    "project": PROJECT,
    "repository": REPOSITORY,
    "repo_path": "/placeholder",
    "default_branch": BASE_BRANCH,
    "closure_state_dir": "/placeholder",
    "local_review_ready_commands": ["true"],
    "closure_acceptance_commands": ["true"],
    "infra_retry_budget": 1,
    "stagnation_budget_minutes": 240,
    "heavy_job_limit": 1,
    "verification_command_timeout_seconds": 2,
    "tracking_projection": None,
}

FAKE_GIT = r'''#!/usr/bin/env python3
import json
import os
import sys


def main():
    control = json.loads(open(os.environ["FAKE_GIT_CONTROL"]).read())
    args = sys.argv[1:]
    path = None
    if len(args) >= 2 and args[0] == "-C":
        path = args[1]
        args = args[2:]
    key = " ".join(args)
    if control.get("log_path"):
        with open(control["log_path"], "a") as handle:
            handle.write(json.dumps([path, key]) + "\n")
    fail = control.get("fail")
    if fail and fail.get("command") == key:
        if fail.get("stderr"):
            sys.stderr.write(fail["stderr"])
        return fail.get("exit", 1)
    if key == "worktree list --porcelain":
        by_path = control.get("worktrees_by_path")
        if by_path and path in by_path:
            blocks = by_path[path]
        elif "worktrees" in control:
            blocks = control["worktrees"]
        else:
            head = control.get("worktree", {}).get("head", "a" * 40)
            branch = control.get("worktree", {}).get("branch", "feature/close")
            blocks = [{"path": path, "head": head, "branch": branch}]
        for block in blocks:
            sys.stdout.write("worktree {0}\nHEAD {1}\n".format(block["path"], block["head"]))
            if block.get("detached"):
                sys.stdout.write("detached\n")
            else:
                sys.stdout.write("branch refs/heads/{0}\n".format(block["branch"]))
            sys.stdout.write("\n")
        return 0
    if key == "rev-parse HEAD":
        sys.stdout.write(control.get("rev_parse_head", "a" * 40) + "\n")
        return 0
    if key.startswith("rev-parse origin/"):
        sys.stdout.write(control.get("rev_parse_remote", "a" * 40) + "\n")
        return 0
    if key == "status --porcelain=v1 --untracked-files=all":
        sys.stdout.write(control.get("status", ""))
        return 0
    sys.stderr.write("fake git: unscripted command: {0}\n".format(key))
    return 127


sys.exit(main())
'''

FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys


def main():
    control = json.loads(open(os.environ["FAKE_GH_CONTROL"]).read())
    args = sys.argv[1:]
    if len(args) >= 6 and args[:2] == ["pr", "view"]:
        number = int(args[2])
        repository = args[4]
        if control.get("stderr"):
            sys.stderr.write(control["stderr"])
            return control.get("exit", 1)
        data = dict(control.get("data", {}))
        data.setdefault("number", number)
        data.setdefault("headRefName", "feature/close")
        data.setdefault("baseRefName", "develop")
        data.setdefault("headRefOid", "a" * 40)
        data.setdefault("isDraft", False)
        data.setdefault("state", "OPEN")
        data.setdefault("url", "https://github.com/{0}/pull/{1}".format(repository, number))
        data.setdefault("statusCheckRollup", [])
        sys.stdout.write(json.dumps(data))
        return 0
    if len(args) >= 6 and args[:2] == ["issue", "view"]:
        number = int(args[2])
        repository = args[4]
        if control.get("issue_stderr"):
            sys.stderr.write(control["issue_stderr"])
            return control.get("issue_exit", 1)
        issues = control.get("issues")
        data = (issues or {}).get(str(number))
        if data is None:
            sys.stderr.write("HTTP 404: issue not found")
            return 1
        data = dict(data)
        data.setdefault("number", number)
        data.setdefault("state", "OPEN")
        data.setdefault("url", "https://github.com/{0}/issues/{1}".format(repository, number))
        sys.stdout.write(json.dumps(data))
        return 0
    sys.stderr.write("fake gh: unscripted command\n")
    return 127


sys.exit(main())
'''

FAKE_ADAPTER = r'''#!/usr/bin/env python3
import json
import os
import sys


def main():
    with open(os.environ["FAKE_ADAPTER_LOG"], "a") as handle:
        handle.write(json.dumps(sys.argv) + "\n")
    mode = sys.argv[sys.argv.index("--mode") + 1]
    if os.environ.get("FAKE_ADAPTER_STDERR"):
        sys.stderr.write(os.environ["FAKE_ADAPTER_STDERR"])
    if os.environ.get("FAKE_ADAPTER_EXIT"):
        sys.exit(int(os.environ["FAKE_ADAPTER_EXIT"]))
    if os.environ.get("FAKE_ADAPTER_SLEEP"):
        import time

        time.sleep(float(os.environ["FAKE_ADAPTER_SLEEP"]))
    raw_file = os.environ.get("FAKE_ADAPTER_RAW_FILE")
    if raw_file:
        with open(raw_file, "rb") as handle:
            sys.stdout.buffer.write(handle.read())
    else:
        raw = os.environ.get("FAKE_ADAPTER_OUTPUT")
        if raw:
            out = json.loads(raw)
        else:
            out = {"schema_version": 1, "applied": mode == "apply", "changes": []}
        sys.stdout.write(json.dumps(out))


sys.exit(main())
'''


_PIPE_HOLDER_SCRIPT = r'''#!/usr/bin/env python3
import os
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
)
with open(os.environ["ADAPTER_PIDFILE"], "w") as handle:
    handle.write(str(child.pid))
'''

_STDOUT_OVERFLOW_SCRIPT = r'''#!/usr/bin/env python3
import os
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
)
with open(os.environ["ADAPTER_PIDFILE"], "w") as handle:
    handle.write(str(child.pid))
sys.stdout.buffer.write(b"x" * 70000)
sys.stdout.buffer.flush()
while True:
    time.sleep(1)
'''

_STDERR_OVERFLOW_SCRIPT = r'''#!/usr/bin/env python3
import os
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
)
with open(os.environ["ADAPTER_PIDFILE"], "w") as handle:
    handle.write(str(child.pid))
sys.stderr.write("y" * 5000)
sys.stderr.flush()
while True:
    time.sleep(1)
'''

_TIMEOUT_CHILD_SCRIPT = r'''#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); import time; time.sleep(120)",
    ],
)
with open(os.environ["ADAPTER_PIDFILE"], "w") as handle:
    handle.write(str(child.pid))
time.sleep(120)
'''


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pctest-", dir=DURABLE_TMP)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.bin_dir = os.path.join(self.root, "bin")
        os.makedirs(self.bin_dir)
        self._write_fake("git", FAKE_GIT)
        self._write_fake("gh", FAKE_GH)
        self._write_fake("adapter", FAKE_ADAPTER)
        self.repo_path = os.path.join(self.root, "repo")
        os.makedirs(self.repo_path)
        self.pr_worktree = os.path.join(self.root, "elsewhere", "pr-worktree")
        os.makedirs(self.pr_worktree)
        self.state_dir = os.path.join(self.root, "state")
        self.git_control = os.path.join(self.root, "git-control.json")
        self.gh_control = os.path.join(self.root, "gh-control.json")
        self.git_log = os.path.join(self.root, "git-log.jsonl")
        self.adapter_log = os.path.join(self.root, "adapter-log.jsonl")
        self._write_json(self.git_control, {})
        self._write_json(self.gh_control, {})
        self.marker = os.path.join(self.root, "marker")

    def _write_fake(self, name, script):
        path = os.path.join(self.bin_dir, name)
        with open(path, "w") as handle:
            handle.write(script)
        os.chmod(path, stat.S_IRWXU)
        return path

    def _write_json(self, path, data):
        with open(path, "w") as handle:
            json.dump(data, handle)

    def set_git(self, **overrides):
        control = {}
        head = overrides.get("head", COMMIT_A)
        branch = overrides.get("branch", BRANCH)
        anchor_block = {"path": self.repo_path, "head": head, "branch": BASE_BRANCH}
        pr_block = {"path": self.pr_worktree, "head": head, "branch": branch}
        if "worktrees" in overrides:
            control["worktrees"] = overrides["worktrees"]
        elif "worktrees_by_path" in overrides:
            control["worktrees_by_path"] = overrides["worktrees_by_path"]
        else:
            control["worktrees"] = [anchor_block, pr_block]
        if "remote" in overrides:
            control["rev_parse_remote"] = overrides["remote"]
        if "local" in overrides:
            control["rev_parse_head"] = overrides["local"]
        if "status" in overrides:
            control["status"] = overrides["status"]
        control["log_path"] = self.git_log
        self._write_json(self.git_control, control)

    def git_calls(self):
        if not os.path.exists(self.git_log):
            return []
        with open(self.git_log) as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def set_git_failure(self, command, stderr, exit_code=128):
        self._write_json(
            self.git_control,
            {"fail": {"command": command, "stderr": stderr, "exit": exit_code}},
        )

    def set_gh(self, **overrides):
        issues = overrides.pop("issues", None)
        control = {"data": overrides}
        if issues is not None:
            control["issues"] = issues
        self._write_json(self.gh_control, control)

    def set_gh_issues(self, issues, issue_stderr=None, issue_exit=1):
        control = {"issues": issues}
        if issue_stderr is not None:
            control["issue_stderr"] = issue_stderr
            control["issue_exit"] = issue_exit
        self._write_json(self.gh_control, control)

    def set_gh_failure(self, stderr, exit_code=1):
        self._write_json(self.gh_control, {"stderr": stderr, "exit": exit_code})

    def write_config(self, overrides=None, path=None):
        config = dict(BASE_CONFIG)
        config["repo_path"] = self.repo_path
        config["closure_state_dir"] = self.state_dir
        if overrides:
            config.update(overrides)
        target = path or os.path.join(self.root, "closure.json")
        self._write_json(target, config)
        return target

    def write_review(self, commit=COMMIT_A, verdict="APPROVED", **overrides):
        record = {
            "schema_version": 1,
            "repository": REPOSITORY,
            "pr_number": PR,
            "reviewed_branch": BRANCH,
            "reviewed_commit": commit,
            "base_commit": "b" * 40,
            "implementer_family": "deepseek",
            "reviewer_family": "claude",
            "local_evidence": ["tests:tools/tests/test_cli.py"],
            "ci_evidence": ["ci:pr-check/run-1"],
            "verdict": verdict,
            "findings": [],
            "intentionally_not_findings": [],
        }
        record.update(overrides)
        path = os.path.join(self.root, "review.json")
        self._write_json(path, record)
        return path

    def run_cli(self, *args, extra_env=None, cwd=None):
        env = dict(os.environ)
        env["PATH"] = self.bin_dir + os.pathsep + env.get("PATH", "")
        env["FAKE_GIT_CONTROL"] = self.git_control
        env["FAKE_GH_CONTROL"] = self.gh_control
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, str(PR_CLOSURE)] + list(args),
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
        )
        return proc

    def read_events(self):
        path = os.path.join(self.state_dir, PROJECT, str(PR), "events.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def read_events_raw(self):
        path = os.path.join(self.state_dir, PROJECT, str(PR), "events.jsonl")
        if not os.path.exists(path):
            return ""
        with open(path) as handle:
            return handle.read()

    def verification_path(self, commit):
        """The single verification attempt path for ``commit`` under the
        append-only ``verification/<commit>/<config-digest>/<attempt>.json``
        layout (T6L-F2), or a non-existent candidate when none exists."""
        base = os.path.join(self.state_dir, PROJECT, str(PR), "verification", commit)
        if not os.path.isdir(base):
            return os.path.join(base, "no-attempt.json")
        paths = sorted(
            os.path.join(dirpath, name)
            for dirpath, dirnames, filenames in os.walk(base)
            for name in filenames
        )
        if len(paths) == 1:
            return paths[0]
        return os.path.join(base, "no-attempt.json")

    def adapter_env(self, output=None, exit_code=None, stderr=None, sleep=None, raw_file=None):
        env = {
            "FAKE_ADAPTER_LOG": self.adapter_log,
        }
        if output is not None:
            env["FAKE_ADAPTER_OUTPUT"] = json.dumps(output)
        if exit_code is not None:
            env["FAKE_ADAPTER_EXIT"] = str(exit_code)
        if stderr is not None:
            env["FAKE_ADAPTER_STDERR"] = stderr
        if sleep is not None:
            env["FAKE_ADAPTER_SLEEP"] = str(sleep)
        if raw_file is not None:
            env["FAKE_ADAPTER_RAW_FILE"] = raw_file
        return env

    def adapter_calls(self):
        if not os.path.exists(self.adapter_log):
            return []
        with open(self.adapter_log) as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def prepare_projection(self, mapping="trello:publyapp", adapter=True):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        overrides = {"tracking_projection": mapping}
        config = self.write_config(overrides=overrides)
        args = ["sync", "--config", config, "--pr", str(PR)]
        if adapter:
            args += ["--projection-adapter", os.path.join(self.bin_dir, "adapter")]
        return config, args

    def prepare_approved(self, verdict="APPROVED", **review_overrides):
        """Full green pipeline: verification (both phases) plus a bound review."""
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        review = self.write_review(commit=COMMIT_A, verdict=verdict, **review_overrides)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return config

    def write_follow_up_review(self, verdict="APPROVED_WITH_FOLLOW_UPS", issue_numbers=(101,)):
        findings = [
            {
                "id": "F-{0}".format(index),
                "root_cause": "docs gap",
                "severity": "MINOR",
                "disposition": "FOLLOW_UP_ISSUE",
                "scope": "docs",
                "summary": "schedule a follow-up",
                "evidence": ["review:framework-task6"],
                "follow_up_issue": number,
            }
            for index, number in enumerate(issue_numbers, start=1)
        ]
        return self.write_review(commit=COMMIT_A, verdict=verdict, findings=findings)


class CliExitCodeTests(CliTestCase):
    def test_help_exits_zero(self):
        proc = self.run_cli("--help")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("usage", proc.stdout.lower())

    def test_invalid_subcommand_exits_two(self):
        proc = self.run_cli("bogus-command")
        self.assertEqual(2, proc.returncode)

    def test_missing_required_option_exits_two(self):
        proc = self.run_cli("status")
        self.assertEqual(2, proc.returncode)

    def test_invalid_config_exits_two(self):
        self.set_git()
        self.set_gh()
        config = self.write_config(overrides={"repo_path": "relative/path"})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(2, proc.returncode)
        self.assertIn("repo_path", proc.stderr)

    def test_invalid_config_creates_no_state_or_evidence_path(self):
        self.set_git()
        self.set_gh()
        config = self.write_config(overrides={"closure_state_dir": "relative/state"})
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(2, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))
        self.assertFalse(os.path.exists(os.path.join(self.repo_path, "verification")))

    def test_source_unavailable_exits_three(self):
        self.set_git()
        self.set_gh_failure("gh pr view failed", exit_code=1)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)

    def test_transition_denied_exits_four(self):
        self.set_git()
        self.set_gh(statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "APPROVED")
        self.assertEqual(4, proc.returncode)

    def test_verification_command_failure_exits_five(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(overrides={"local_review_ready_commands": ["exit 7"]})
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)

    def test_lease_unavailable_exits_six(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        lease = HeavyJobLease(self.state_dir, PROJECT, PR, ["sh", "-c", "true"])
        lease.acquire()
        try:
            proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
            self.assertEqual(6, proc.returncode)
            self.assertIn("lease", proc.stderr)
        finally:
            lease.release()

    def test_successful_commands_exit_zero(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)


class StatusCommandTests(CliTestCase):
    def test_status_read_only_repeated_runs_change_nothing(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc1 = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc1.returncode, proc1.stderr)
        events_before = self.read_events()
        files_before = sorted(
            os.path.relpath(os.path.join(dirpath, name), self.state_dir)
            for dirpath, dirnames, filenames in os.walk(self.state_dir)
            for name in filenames
        )
        proc2 = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc2.returncode, proc2.stderr)
        proc3 = self.run_cli("status", "--config", config, "--pr", str(PR), "--json")
        self.assertEqual(0, proc3.returncode, proc3.stderr)
        self.assertEqual(events_before, self.read_events())
        files_after = sorted(
            os.path.relpath(os.path.join(dirpath, name), self.state_dir)
            for dirpath, dirnames, filenames in os.walk(self.state_dir)
            for name in filenames
        )
        self.assertEqual(files_before, files_after)

    def test_status_json_is_machine_readable(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR), "--json")
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(PROJECT, payload["project"])
        self.assertEqual(PR, payload["pr"])
        self.assertIn(payload["state"], {
            "UNVERIFIED", "CI_RED", "CI_INFRA_RETRY", "FIXING", "LOCAL_VERIFY",
            "REVIEW_READY", "REVIEWING", "CHANGES_REQUIRED", "DESIGN_RESET",
            "FOLLOW_UP_FILING", "APPROVED_WITH_FOLLOW_UPS", "APPROVED",
            "NEEDS_OWNER", "STALLED",
        })
        self.assertEqual([], [line for line in proc.stderr.splitlines() if line])

    def test_status_wires_required_checks_policy_into_ci_classification(self):
        self.set_git()
        rollup = [
            {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ]
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=rollup)
        config = self.write_config(overrides={"ci_required_checks": ["ci"]})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR), "--json")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("PASSING", json.loads(proc.stdout)["ci_state"])

    def test_status_without_policy_keeps_all_rollup_strict_behavior(self):
        self.set_git()
        rollup = [
            {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ]
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=rollup)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR), "--json")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("UNKNOWN", json.loads(proc.stdout)["ci_state"])

    def test_status_text_reports_state(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: REVIEW_READY", proc.stdout)
        self.assertIn("dispatch_review", proc.stdout)

    def test_status_never_approves_on_missing_or_contradictory_evidence(self):
        self.set_git()
        self.set_gh()
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("APPROVED", proc.stdout.split("state: ")[1].splitlines()[0])
        self.set_git(local=COMMIT_A, remote=COMMIT_B)
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: UNVERIFIED", proc.stdout)

    def test_dirty_worktree_is_unverified(self):
        self.set_git(status="?? untracked.py\n")
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: UNVERIFIED", proc.stdout)


class ImportReviewTests(CliTestCase):
    def prepare_tip(self, commit=COMMIT_A):
        self.set_git()
        self.set_gh(headRefOid=commit, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        return config

    def test_import_review_at_exact_tip_succeeds(self):
        config = self.prepare_tip()
        review = self.write_review(commit=COMMIT_A)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        events = self.read_events()
        self.assertTrue(any(e["event_type"] == "review" for e in events))
        review_dir = os.path.join(self.state_dir, PROJECT, str(PR), "reviews", COMMIT_A)
        self.assertTrue(os.path.isfile(os.path.join(review_dir, "review.json")))
        status = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("state: APPROVED", status.stdout)

    def test_import_review_for_stale_commit_is_rejected(self):
        config = self.prepare_tip(commit=COMMIT_A)
        review = self.write_review(commit=COMMIT_B)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(2, proc.returncode)
        self.assertNotIn(COMMIT_B, proc.stdout)
        self.assertFalse(
            os.path.exists(os.path.join(self.state_dir, PROJECT, str(PR), "reviews", COMMIT_B))
        )

    def test_import_review_without_durable_tip_is_rejected(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        review = self.write_review(commit=COMMIT_A)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(2, proc.returncode)
        self.assertFalse(
            os.path.exists(os.path.join(self.state_dir, PROJECT, str(PR), "reviews"))
        )

    def test_import_review_rejects_same_family_and_malformed_artifact(self):
        config = self.prepare_tip()
        review = self.write_review(commit=COMMIT_A, reviewer_family="deepseek-v4-flash")
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(2, proc.returncode)
        bad_path = os.path.join(self.root, "not-json.json")
        with open(bad_path, "w") as handle:
            handle.write("{not json")
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", bad_path)
        self.assertEqual(2, proc.returncode)
        self.assertFalse(
            os.path.exists(os.path.join(self.state_dir, PROJECT, str(PR), "reviews"))
        )

    def test_import_review_rejects_wrong_branch_or_repository_binding(self):
        config = self.prepare_tip()
        review = self.write_review(commit=COMMIT_A, reviewed_branch="other/branch")
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(2, proc.returncode)
        review = self.write_review(commit=COMMIT_A, repository="other/owner")
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(2, proc.returncode)

    def test_import_review_duplicate_id_with_different_bytes_exits_two(self):
        config = self.prepare_tip()
        review = self.write_review(commit=COMMIT_A)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        review2 = self.write_review(commit=COMMIT_A, verdict="APPROVED_WITH_FOLLOW_UPS")
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review2)
        self.assertEqual(2, proc.returncode)


class RecordVerificationTests(CliTestCase):
    def test_verification_success_records_exact_commit_and_status(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(overrides={"local_review_ready_commands": ["true"]})
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        record_path = self.verification_path(COMMIT_A)
        self.assertTrue(os.path.isfile(record_path))
        with open(record_path) as handle:
            record = json.load(handle)
        self.assertEqual(COMMIT_A, record["commit"])
        self.assertEqual("PASSED", record["outcome"])
        self.assertEqual(2, len(record["commands"]))
        self.assertEqual(
            hashlib.sha256(b"true").hexdigest(), record["commands"][0]["command_digest"]
        )
        self.assertEqual("local_review_ready:1", record["commands"][0]["command_label"])
        self.assertEqual("closure_acceptance:2", record["commands"][1]["command_label"])
        self.assertEqual(0, record["commands"][0]["exit_status"])
        self.assertNotIn("true", record["commands"][0])
        events = self.read_events()
        self.assertTrue(any(e["event_type"] == "commit" and e["commit"] == COMMIT_A for e in events))
        self.assertTrue(any(e["event_type"] == "verification" for e in events))
        verification_events = [e for e in events if e["event_type"] == "verification"]
        self.assertEqual(1, len(verification_events))
        self.assertEqual(
            hashlib.sha256(
                Path(self.verification_path(COMMIT_A)).read_bytes()
            ).hexdigest(),
            verification_events[0]["content_digest"],
        )

    def test_verification_records_phase_on_every_command(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["printf local > " + self.marker],
                "closure_acceptance_commands": ["printf acceptance >> " + self.marker],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        with open(self.verification_path(COMMIT_A)) as handle:
            record = json.load(handle)
        self.assertEqual(
            ["local_review_ready", "closure_acceptance"],
            [run["phase"] for run in record["commands"]],
        )
        with open(self.marker) as handle:
            self.assertEqual("localacceptance", handle.read())

    def test_verification_stops_on_failure_and_never_records_green(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={"local_review_ready_commands": ["true", "exit 7", "echo should-not-run"]}
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        self.assertFalse(os.path.isfile(self.verification_path(COMMIT_A)))
        events = self.read_events()
        failed = [e for e in events if e["event_type"] == "verification"]
        self.assertEqual(1, len(failed))
        self.assertEqual("FAILED", failed[0]["outcome"])
        self.assertEqual([0, 7], [c["exit_status"] for c in failed[0]["commands"]])
        self.assertEqual(2, len(failed[0]["commands"]))
        status = self.run_cli("status", "--config", config, "--pr", str(PR), "--json")
        self.assertEqual(0, status.returncode, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(False, payload["local_verification"])

    def test_acceptance_failure_never_approves_even_with_bound_review(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["true"],
                "closure_acceptance_commands": ["exit 7"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        self.assertFalse(os.path.isfile(self.verification_path(COMMIT_A)))
        events = self.read_events()
        failed = [e for e in events if e["event_type"] == "verification"]
        self.assertEqual("FAILED", failed[0]["outcome"])
        self.assertEqual(
            ["local_review_ready", "closure_acceptance"],
            [c["phase"] for c in failed[0]["commands"]],
        )
        self.assertEqual([0, 7], [c["exit_status"] for c in failed[0]["commands"]])
        review = self.write_review(commit=COMMIT_A)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: LOCAL_VERIFY", proc.stdout)
        self.assertNotIn("APPROVED", proc.stdout.split("state: ")[1].splitlines()[0])

    def test_verification_command_timeout_fails_and_cleans_up_processes(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        self._write_fake("timeout-child", _TIMEOUT_CHILD_SCRIPT)
        child_pid_file = os.path.join(self.root, "verification-child.pid")
        config = self.write_config(
            overrides={
                "verification_command_timeout_seconds": 1,
                "local_review_ready_commands": [os.path.join(self.bin_dir, "timeout-child")],
            }
        )
        start = time.time()
        proc = self.run_cli(
            "record-verification",
            "--config",
            config,
            "--pr",
            str(PR),
            extra_env={"ADAPTER_PIDFILE": child_pid_file},
        )
        elapsed = time.time() - start
        self.assertEqual(5, proc.returncode)
        self.assertLess(elapsed, 5.0)
        self.assertFalse(os.path.isfile(self.verification_path(COMMIT_A)))
        events = self.read_events()
        failed = [e for e in events if e["event_type"] == "verification"]
        self.assertEqual(1, len(failed))
        self.assertEqual("FAILED", failed[0]["outcome"])
        self.assertEqual(["local_review_ready"], [c["phase"] for c in failed[0]["commands"]])
        self.assertEqual(1, len(failed[0]["commands"]))
        self.assertNotEqual(0, failed[0]["commands"][0]["exit_status"])
        self.assertTrue(failed[0]["commands"][0]["timed_out"])
        self.assertEqual("timeout", failed[0]["commands"][0]["failure_reason"])
        deadline = time.time() + 2
        while time.time() < deadline and not os.path.exists(child_pid_file):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(child_pid_file))
        with open(child_pid_file) as handle:
            child_pid = int(handle.read())
        self.assertFalse(self._process_exists(child_pid))

    @staticmethod
    def _process_exists(pid: int) -> bool:
        try:
            with open("/proc/{0}/stat".format(pid), "r", encoding="utf-8") as handle:
                state = handle.read().split()[2]
            return state != "Z"
        except OSError:
            return False

    def test_full_pass_control_approves_when_other_evidence_valid(self):
        config = self.prepare_approved(verdict="APPROVED")
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED", proc.stdout)
        with open(self.verification_path(COMMIT_A)) as handle:
            record = json.load(handle)
        self.assertEqual(
            ["local_review_ready", "closure_acceptance"],
            [run["phase"] for run in record["commands"]],
        )

    def test_verification_refuses_unpushed_commit(self):
        self.set_git(local=COMMIT_A, remote=COMMIT_B)
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_verification_refuses_head_mismatch(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_B, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_verification_records_commands_as_single_shell_argv(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        command = "printf '%s\\n' 'a b c' > " + self.marker
        config = self.write_config(overrides={"local_review_ready_commands": [command]})
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        with open(self.marker) as handle:
            self.assertEqual("a b c\n", handle.read())
        with open(self.verification_path(COMMIT_A)) as handle:
            record = json.load(handle)
        self.assertEqual(
            hashlib.sha256(command.encode("utf-8")).hexdigest(),
            record["commands"][0]["command_digest"],
        )
        self.assertNotIn(command, record["commands"][0]["command_label"])

    def test_verification_command_text_never_rewritten_or_split(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        command = "echo '; rm -rf '${HOME} > " + self.marker
        config = self.write_config(overrides={"local_review_ready_commands": [command]})
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        with open(self.verification_path(COMMIT_A)) as handle:
            record = json.load(handle)
        self.assertEqual(
            hashlib.sha256(command.encode("utf-8")).hexdigest(),
            record["commands"][0]["command_digest"],
        )
        self.assertNotIn("rm -rf", record["commands"][0]["command_label"])
        with open(self.marker) as handle:
            self.assertIn("; rm -rf", handle.read())

    def test_verification_runs_all_configured_commands_in_order(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": [
                    "printf one > " + self.marker,
                    "printf two >> " + self.marker,
                ]
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        with open(self.marker) as handle:
            self.assertEqual("onetwo", handle.read())

    def test_failure_evidence_never_persists_secret_command_text(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["echo '--token ab12'; exit 9"],
                "closure_acceptance_commands": ["true"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        self.assertNotIn("ab12", proc.stderr)
        self.assertNotIn("ab12", self.read_events_raw())

    def test_verification_lease_metadata_names_the_job(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(overrides={"local_review_ready_commands": ["true"]})
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        meta_path = os.path.join(self.state_dir, PROJECT, str(PR), "heavy-job.meta.json")
        self.assertFalse(os.path.exists(meta_path))


class WorkerEvidenceBoundaryTests(CliTestCase):
    """T8B3: exit-zero but empty or markerless worker output is rejected, and
    the verification pipeline binds each command's own exit status so the
    command that actually failed can never be recorded green."""

    # -- case 1: exit-zero but empty or markerless worker output ----------

    def test_sync_exit_zero_empty_worker_output_fails_closed(self):
        config, args = self.prepare_projection()
        raw = os.path.join(self.root, "empty.out")
        with open(raw, "w") as handle:
            handle.write("")
        proc = self.run_cli(*args, extra_env=self.adapter_env(raw_file=raw))
        self.assertEqual(5, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertIn("projection adapter", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(1, len(self.adapter_calls()))

    def test_sync_exit_zero_markerless_text_worker_output_fails_closed(self):
        config, args = self.prepare_projection()
        raw = os.path.join(self.root, "markerless-text.out")
        with open(raw, "w") as handle:
            handle.write("ok\n")
        proc = self.run_cli(*args, extra_env=self.adapter_env(raw_file=raw))
        self.assertEqual(5, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertIn("projection adapter", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(1, len(self.adapter_calls()))

    def test_sync_exit_zero_markerless_json_worker_output_fails_closed(self):
        config, args = self.prepare_projection()
        raw = os.path.join(self.root, "markerless-json.out")
        with open(raw, "w") as handle:
            json.dump({"changes": []}, handle)
        proc = self.run_cli(*args, extra_env=self.adapter_env(raw_file=raw))
        self.assertEqual(5, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertIn("projection adapter", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(1, len(self.adapter_calls()))

    # -- case 2: per-command exit binding; never green for the failed one --

    def test_failing_acceptance_command_binds_its_own_exit_and_never_green(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["true"],
                "closure_acceptance_commands": ["exit 7"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        self.assertFalse(os.path.isfile(self.verification_path(COMMIT_A)))
        failed = [e for e in self.read_events() if e["event_type"] == "verification"]
        self.assertEqual(1, len(failed))
        self.assertEqual("FAILED", failed[0]["outcome"])
        self.assertEqual(
            ["local_review_ready", "closure_acceptance"],
            [c["phase"] for c in failed[0]["commands"]],
        )
        self.assertEqual([0, 7], [c["exit_status"] for c in failed[0]["commands"]])
        self.assertEqual(
            hashlib.sha256(b"true").hexdigest(),
            failed[0]["commands"][0]["command_digest"],
        )
        self.assertEqual(
            hashlib.sha256(b"exit 7").hexdigest(),
            failed[0]["commands"][1]["command_digest"],
        )
        self.assertEqual(
            hashlib.sha256(b"exit 7").hexdigest(),
            failed[0]["failed_command_digest"],
        )
        status = self.run_cli("status", "--config", config, "--pr", str(PR), "--json")
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertFalse(json.loads(status.stdout)["local_verification"])

    def test_failing_local_review_command_binds_its_own_exit_and_never_green(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["exit 9"],
                "closure_acceptance_commands": ["true"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        self.assertFalse(os.path.isfile(self.verification_path(COMMIT_A)))
        failed = [e for e in self.read_events() if e["event_type"] == "verification"]
        self.assertEqual(1, len(failed))
        self.assertEqual("FAILED", failed[0]["outcome"])
        self.assertEqual(
            ["local_review_ready"],
            [c["phase"] for c in failed[0]["commands"]],
        )
        self.assertEqual([9], [c["exit_status"] for c in failed[0]["commands"]])
        self.assertEqual(
            hashlib.sha256(b"exit 9").hexdigest(),
            failed[0]["commands"][0]["command_digest"],
        )
        self.assertEqual(
            hashlib.sha256(b"exit 9").hexdigest(),
            failed[0]["failed_command_digest"],
        )
        status = self.run_cli("status", "--config", config, "--pr", str(PR), "--json")
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertFalse(json.loads(status.stdout)["local_verification"])


class RecordInfraFailureTests(CliTestCase):
    def test_record_infra_failure_stores_bounded_commit_bound_evidence(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli(
            "record-infra-failure",
            "--config", config,
            "--pr", str(PR),
            "--failed-job", "build",
            "--test-steps-not-started", "Run tests",
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        events = self.read_events()
        infra = [e for e in events if e["event_type"] == "INFRA_FAILURE"]
        self.assertEqual(1, len(infra))
        self.assertEqual(COMMIT_A, infra[0]["commit"])
        self.assertEqual("build", infra[0]["failed_job"])
        self.assertEqual(["Run tests"], infra[0]["test_steps_not_started"])

    def test_record_infra_failure_rejects_blank_job_or_steps(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        for args in (
            ("--failed-job", "   ", "--test-steps-not-started", "Run tests"),
            ("--failed-job", "build", "--test-steps-not-started", "  "),
        ):
            with self.subTest(args=args):
                proc = self.run_cli(
                    "record-infra-failure", "--config", config, "--pr", str(PR), *args
                )
                self.assertEqual(2, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_record_infra_failure_refuses_unpushed_commit(self):
        self.set_git(local=COMMIT_A, remote=COMMIT_B)
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli(
            "record-infra-failure",
            "--config", config,
            "--pr", str(PR),
            "--failed-job", "build", "--test-steps-not-started", "Run tests",
        )
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))


class WorktreeResolutionTests(CliTestCase):
    """C6C-F1: the PR worktree is resolved from ``git worktree list`` by the
    live head branch; the configured default worktree is never inspected."""

    def test_status_reads_never_touch_the_anchor_worktree(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        anchor_lists = 0
        for path, key in self.git_calls():
            if key == "worktree list --porcelain":
                if path == self.repo_path:
                    anchor_lists += 1
                else:
                    self.assertEqual(self.pr_worktree, path)
            else:
                self.assertEqual(self.pr_worktree, path, key)
        self.assertGreaterEqual(anchor_lists, 1)

    def test_record_verification_and_infra_bind_the_pr_worktree(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli(
            "record-infra-failure", "--config", config, "--pr", str(PR),
            "--failed-job", "build", "--test-steps-not-started", "Run tests",
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        anchor_lists = 0
        for path, key in self.git_calls():
            if key == "worktree list --porcelain":
                if path == self.repo_path:
                    anchor_lists += 1
                else:
                    self.assertEqual(self.pr_worktree, path)
            else:
                self.assertEqual(self.pr_worktree, path, key)
        self.assertGreaterEqual(anchor_lists, 1)

    def test_sync_and_check_transition_bind_the_pr_worktree(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "REVIEW_READY")
        self.assertEqual(4, proc.returncode)
        config = self.write_config(overrides={"tracking_projection": "trello:publyapp"})
        proc = self.run_cli(
            "sync", "--config", config, "--pr", str(PR),
            "--projection-adapter", os.path.join(self.bin_dir, "adapter"),
            extra_env=self.adapter_env(),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        anchor_lists = 0
        for path, key in self.git_calls():
            if key == "worktree list --porcelain":
                if path == self.repo_path:
                    anchor_lists += 1
                else:
                    self.assertEqual(self.pr_worktree, path)
            else:
                self.assertEqual(self.pr_worktree, path, key)
        self.assertGreaterEqual(anchor_lists, 1)

    def test_missing_worktree_binding_fails_closed_with_zero_writes(self):
        self.set_git(worktrees=[{"path": self.repo_path, "head": COMMIT_A, "branch": BASE_BRANCH}])
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("worktree", proc.stderr)
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_duplicate_worktree_binding_fails_closed(self):
        self.set_git(
            worktrees=[
                {"path": self.repo_path, "head": COMMIT_A, "branch": BASE_BRANCH},
                {"path": self.pr_worktree, "head": COMMIT_A, "branch": BRANCH},
                {"path": os.path.join(self.root, "clone-two"), "head": COMMIT_A, "branch": BRANCH},
            ]
        )
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_detached_worktree_never_matches(self):
        self.set_git(
            worktrees=[
                {"path": self.repo_path, "head": COMMIT_A, "branch": BASE_BRANCH},
                {"path": self.pr_worktree, "head": COMMIT_A, "detached": True},
            ]
        )
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)

    def test_checked_out_branch_must_stay_equal_after_resolution(self):
        self.set_git(
            worktrees_by_path={
                self.repo_path: [
                    {"path": self.repo_path, "head": COMMIT_A, "branch": BASE_BRANCH},
                    {"path": self.pr_worktree, "head": COMMIT_A, "branch": BRANCH},
                ],
                self.pr_worktree: [
                    {"path": self.repo_path, "head": COMMIT_A, "branch": BASE_BRANCH},
                    {"path": self.pr_worktree, "head": COMMIT_A, "branch": "moved-branch"},
                ],
            }
        )
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("branch", proc.stderr)
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_base_branch_mismatch_fails_closed(self):
        self.set_git()
        self.set_gh(baseRefName="main", headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("base", proc.stderr)
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_non_open_pr_fails_closed_everywhere(self):
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state):
                self.set_git()
                self.set_gh(state=state, headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
                config = self.write_config()
                proc = self.run_cli("status", "--config", config, "--pr", str(PR))
                self.assertEqual(3, proc.returncode)
                proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
                self.assertEqual(3, proc.returncode)
                self.assertFalse(os.path.exists(self.state_dir))

    def test_draft_pr_never_derives_approval(self):
        self.set_git()
        self.set_gh(isDraft=True, headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: UNVERIFIED", proc.stdout)
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        review = self.write_review(commit=COMMIT_A)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: UNVERIFIED", proc.stdout)
        self.assertNotIn("APPROVED", proc.stdout.split("state: ")[1].splitlines()[0])
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "APPROVED")
        self.assertEqual(4, proc.returncode)

    def test_import_review_refuses_non_open_pr(self):
        self.set_git()
        self.set_gh(state="CLOSED", headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        review = self.write_review(commit=COMMIT_A)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))


class EventBoundArtifactTests(CliTestCase):
    """C6C-F4: only event-bound durable artifacts are authority."""

    def prepare_commit_only(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        store = RunStore(self.state_dir, PROJECT, PR)
        store.record_commit(COMMIT_A, str(store.events_path))
        return config

    def test_orphan_verification_artifact_fails_closed(self):
        config = self.prepare_commit_only()
        path = os.path.join(os.path.dirname(self.verification_path(COMMIT_A)), "orphan-config", "orphan.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            json.dump({"schema_version": 1, "outcome": "PASSED", "commit": COMMIT_A}, handle)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("verification", proc.stderr)

    def test_orphan_review_artifact_fails_closed(self):
        config = self.prepare_commit_only()
        review_dir = os.path.join(self.state_dir, PROJECT, str(PR), "reviews", COMMIT_A)
        os.makedirs(review_dir)
        with open(os.path.join(review_dir, "orphan.json"), "w") as handle:
            json.dump({"schema_version": 1}, handle)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("review", proc.stderr)

    def test_symlink_verification_artifact_fails_closed(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        path = self.verification_path(COMMIT_A)
        os.unlink(path)
        decoy = os.path.join(self.root, "decoy.json")
        with open(decoy, "w") as handle:
            json.dump({"schema_version": 1, "outcome": "PASSED", "commit": COMMIT_A}, handle)
        os.symlink(decoy, path)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)

    def test_missing_verification_artifact_never_approves(self):
        config = self.prepare_commit_only()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: LOCAL_VERIFY", proc.stdout)
        self.assertNotIn("APPROVED", proc.stdout.split("state: ")[1].splitlines()[0])

    def test_bound_verification_and_review_control_approves(self):
        config = self.prepare_approved(verdict="APPROVED")
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED", proc.stdout)


class FollowUpVerificationTests(CliTestCase):
    """C6C-F6: follow-up issues are read live and verified, never inferred."""

    def prepare_follow_up_state(self, issues, state_dir=None, issue_numbers=(101,)):
        self.set_git()
        self.set_gh(
            headRefOid=COMMIT_A,
            statusCheckRollup=PASSING_ROLLUP,
            issues=issues,
        )
        overrides = {}
        if state_dir is not None:
            overrides["closure_state_dir"] = state_dir
        config = self.write_config(overrides=overrides)
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        review = self.write_follow_up_review(issue_numbers=issue_numbers)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return config

    def test_open_follow_up_issue_approves_with_follow_ups(self):
        config = self.prepare_follow_up_state(issues={"101": {"state": "OPEN"}})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED_WITH_FOLLOW_UPS", proc.stdout)

    def test_scheduled_follow_up_issue_is_acceptable(self):
        config = self.prepare_follow_up_state(issues={"101": {"state": "SCHEDULED"}})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED_WITH_FOLLOW_UPS", proc.stdout)

    def test_closed_follow_up_issue_keeps_follow_up_filing(self):
        config = self.prepare_follow_up_state(issues={"101": {"state": "CLOSED"}})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: FOLLOW_UP_FILING", proc.stdout)
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "APPROVED_WITH_FOLLOW_UPS")
        self.assertEqual(4, proc.returncode)

    def test_missing_follow_up_issue_fails_closed(self):
        config = self.prepare_follow_up_state(issues={})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("issue", proc.stderr)

    def test_issue_api_failure_fails_closed(self):
        config = self.prepare_follow_up_state(issues={"101": {"state": "OPEN"}})
        self.set_gh_issues({"101": {"state": "OPEN"}}, issue_stderr="HTTP 500: API down")
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)

    def test_unbound_issue_url_fails_closed(self):
        config = self.prepare_follow_up_state(
            issues={"101": {"state": "OPEN", "url": "https://github.com/evil/repo/issues/101"}}
        )
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)

    def test_unknown_issue_state_fails_closed(self):
        config = self.prepare_follow_up_state(issues={"101": {"state": "DONE"}})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)

    def test_note_only_review_reads_no_issues(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP, issues={})
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        review = self.write_review(
            commit=COMMIT_A,
            verdict="APPROVED",
            findings=[
                {
                    "id": "N-1",
                    "root_cause": "observation",
                    "severity": "NOTE",
                    "disposition": "NOTE_ONLY",
                    "scope": "docs",
                    "summary": "consider documenting",
                    "evidence": ["review:framework-task6"],
                }
            ],
        )
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED", proc.stdout)

    def test_multiple_follow_ups_all_must_be_acceptable(self):
        config = self.prepare_follow_up_state(
            issues={"101": {"state": "OPEN"}, "102": {"state": "OPEN"}},
            issue_numbers=(101, 102),
        )
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED_WITH_FOLLOW_UPS", proc.stdout)
        second_dir = os.path.join(self.root, "state-two")
        config = self.prepare_follow_up_state(
            issues={"101": {"state": "OPEN"}, "102": {"state": "CLOSED"}},
            issue_numbers=(101, 102),
            state_dir=second_dir,
        )
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: FOLLOW_UP_FILING", proc.stdout)


class CheckTransitionTests(CliTestCase):
    def test_matching_nonterminal_state_target_succeeds(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "REVIEW_READY")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state=REVIEW_READY", proc.stdout)
        self.assertIn("target=REVIEW_READY", proc.stdout)
        self.assertIn("allowed=yes", proc.stdout)

    def test_nonterminal_target_mismatch_exits_four(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "APPROVED")
        self.assertEqual(4, proc.returncode)
        self.assertIn("state=REVIEW_READY", proc.stdout)
        self.assertIn("target=APPROVED", proc.stdout)
        self.assertIn("allowed=no", proc.stdout)
        self.assertIn("denied", proc.stderr)

    def test_terminal_state_target_succeeds(self):
        config = self.prepare_approved(verdict="APPROVED")
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "APPROVED")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("allowed=yes", proc.stdout)

    def test_terminal_with_follow_ups_state_target_succeeds(self):
        self.set_git()
        self.set_gh(
            headRefOid=COMMIT_A,
            statusCheckRollup=PASSING_ROLLUP,
            issues={"101": {"state": "OPEN"}},
        )
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        review = self.write_follow_up_review()
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "APPROVED_WITH_FOLLOW_UPS")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("allowed=yes", proc.stdout)

    def test_invalid_state_target_exits_two(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "push")
        self.assertEqual(2, proc.returncode)
        self.assertIn("state", proc.stderr)

    def test_unverified_never_matches_terminal_target(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "APPROVED")
        self.assertEqual(4, proc.returncode)
        self.assertIn("state=UNVERIFIED", proc.stdout)
        self.assertIn("allowed=no", proc.stdout)

    def test_check_transition_source_failure_exits_three(self):
        self.set_git()
        self.set_gh_failure("gh pr view failed")
        config = self.write_config()
        proc = self.run_cli("check-transition", "--config", config, "--pr", str(PR), "--to", "APPROVED")
        self.assertEqual(3, proc.returncode)


class SyncCommandTests(CliTestCase):

    def test_sync_dry_run_with_adapter_reports_proposed_changes(self):
        config, args = self.prepare_projection()
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state=", proc.stdout)
        self.assertIn("projection dry-run: changes=0", proc.stdout)
        self.assertEqual([], [line for line in proc.stderr.splitlines() if line])
        calls = self.adapter_calls()
        self.assertEqual(1, len(calls))
        self.assertEqual("dry-run", calls[0][calls[0].index("--mode") + 1])

    def test_sync_apply_with_adapter_reports_applied(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("projection applied: changes=0", proc.stdout)
        calls = self.adapter_calls()
        self.assertEqual("apply", calls[0][calls[0].index("--mode") + 1])

    def test_sync_gh_failure_never_invokes_projection_adapter(self):
        config, args = self.prepare_projection()
        self.set_gh_failure("GraphQL: Could not resolve to a PullRequest")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(3, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_sync_apply_without_projection_exits_two_and_never_calls_adapter(self):
        config, args = self.prepare_projection(mapping=None)
        args.append("--apply")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(2, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_sync_dry_run_with_null_mapping_reports_no_projection(self):
        config, args = self.prepare_projection(mapping=None)
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("no tracking_projection", proc.stdout)
        self.assertEqual([], self.adapter_calls())

    def test_sync_with_mapping_but_no_adapter_is_refused(self):
        config, args = self.prepare_projection(adapter=False)
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(2, proc.returncode)
        self.assertIn("adapter", proc.stderr)
        self.assertEqual([], self.adapter_calls())
        proc = self.run_cli(*(args + ["--apply"]), extra_env=self.adapter_env())
        self.assertEqual(2, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_sync_apply_without_mapping_but_with_adapter_is_refused(self):
        config, args = self.prepare_projection(mapping=None)
        args.append("--apply")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(2, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_adapter_path_must_be_absolute(self):
        config, args = self.prepare_projection()
        args = args[:-1] + ["--projection-adapter", "relative/adapter"]
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(2, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_adapter_path_must_be_a_regular_non_symlink_executable(self):
        config, args = self.prepare_projection()
        symlink = os.path.join(self.root, "adapter-link")
        os.symlink(os.path.join(self.bin_dir, "adapter"), symlink)
        proc = self.run_cli(*(args[:-2] + ["--projection-adapter", symlink]), extra_env=self.adapter_env())
        self.assertEqual(2, proc.returncode)
        self.assertIn("symlink", proc.stderr)
        proc = self.run_cli(*(args[:-2] + ["--projection-adapter", self.git_control]), extra_env=self.adapter_env())
        self.assertEqual(2, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_adapter_path_must_be_executable(self):
        config, args = self.prepare_projection()
        non_exec = os.path.join(self.bin_dir, "not-executable")
        with open(non_exec, "w") as handle:
            handle.write("#!/bin/true\n")
        os.chmod(non_exec, 0o644)
        proc = self.run_cli(*(args[:-2] + ["--projection-adapter", non_exec]), extra_env=self.adapter_env())
        self.assertEqual(2, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_sync_apply_refuses_unavailable_source_with_zero_adapter_calls(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        self.set_gh_failure("gh pr view failed")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(3, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_sync_apply_refuses_contradictory_sources_with_zero_adapter_calls(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        self.set_git(local=COMMIT_A, remote=COMMIT_B)
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(3, proc.returncode)
        self.assertEqual([], self.adapter_calls())

    def test_sync_apply_adapter_nonzero_exits_five_with_redacted_stderr(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(exit_code=9, stderr="boom --token ab12 failed"),
        )
        self.assertEqual(5, proc.returncode)
        self.assertNotIn("ab12", proc.stderr)
        self.assertEqual(1, len(self.adapter_calls()))

    def test_sync_adapter_malformed_output_fails_closed(self):
        config, args = self.prepare_projection()
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(output={"applied": False, "changes": []}),
        )
        self.assertEqual(5, proc.returncode)
        self.assertIn("schema_version", proc.stderr)

    def test_sync_adapter_missing_output_fails_closed(self):
        config, args = self.prepare_projection()
        proc = self.run_cli(*args, extra_env=self.adapter_env(output=None, exit_code=0))
        self.assertEqual(5, proc.returncode)

    def test_sync_dry_run_wrong_mode_reports_applied_true_fails_closed(self):
        config, args = self.prepare_projection()
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(output={"schema_version": 1, "applied": True, "changes": []}),
        )
        self.assertEqual(5, proc.returncode)
        self.assertIn("dry-run", proc.stderr)

    def test_sync_apply_wrong_mode_reports_applied_false_fails_closed(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(output={"schema_version": 1, "applied": False, "changes": []}),
        )
        self.assertEqual(5, proc.returncode)
        self.assertIn("applied", proc.stderr)

    def test_sync_adapter_result_requires_changes_array(self):
        config, args = self.prepare_projection()
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(output={"schema_version": 1, "applied": False}),
        )
        self.assertEqual(5, proc.returncode)

    def test_projection_mapping_is_never_executed_as_shell(self):
        config, args = self.prepare_projection(mapping="trello:publyapp")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(0, proc.returncode, proc.stderr)
        calls = self.adapter_calls()
        self.assertEqual(1, len(calls))
        argv = calls[0]
        self.assertEqual(os.path.join(self.bin_dir, "adapter"), argv[0])
        self.assertIn("--mapping", argv)
        self.assertEqual("trello:publyapp", argv[argv.index("--mapping") + 1])
        self.assertNotIn("sh", argv)

    def test_adapter_receives_project_pr_and_state_as_distinct_argv(self):
        config, args = self.prepare_projection()
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(0, proc.returncode, proc.stderr)
        argv = self.adapter_calls()[0]
        self.assertIn("--project", argv)
        self.assertEqual(PROJECT, argv[argv.index("--project") + 1])
        self.assertIn("--pr", argv)
        self.assertEqual(str(PR), argv[argv.index("--pr") + 1])
        self.assertIn("--state", argv)
        self.assertIn(argv[argv.index("--state") + 1], {
            "UNVERIFIED", "CI_RED", "CI_INFRA_RETRY", "FIXING", "LOCAL_VERIFY",
            "REVIEW_READY", "REVIEWING", "CHANGES_REQUIRED", "DESIGN_RESET",
            "FOLLOW_UP_FILING", "APPROVED_WITH_FOLLOW_UPS", "APPROVED",
            "NEEDS_OWNER", "STALLED",
        })

    def _plant_verification_root(self, kind):
        base = os.path.join(self.state_dir, PROJECT, str(PR), "verification")
        os.makedirs(base, exist_ok=True)
        target = os.path.join(base, COMMIT_A)
        if kind == "regular-file":
            with open(target, "w") as handle:
                handle.write("not a directory")
        elif kind == "symlink":
            real = os.path.join(self.root, "verification-target")
            os.makedirs(real, exist_ok=True)
            os.symlink(real, target)
        elif kind == "fifo":
            os.mkfifo(target)
        else:
            raise AssertionError("unknown root kind {0!r}".format(kind))
        return target

    def test_sync_apply_refuses_regular_file_verification_root_with_zero_adapter_calls(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        self._plant_verification_root("regular-file")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(3, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertIn("verification", proc.stderr)
        self.assertEqual([], self.adapter_calls())

    def test_sync_apply_refuses_symlink_verification_root_with_zero_adapter_calls(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        self._plant_verification_root("symlink")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(3, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertIn("verification", proc.stderr)
        self.assertEqual([], self.adapter_calls())

    def test_sync_apply_refuses_fifo_verification_root_with_zero_adapter_calls(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        self._plant_verification_root("fifo")
        proc = self.run_cli(*args, extra_env=self.adapter_env())
        self.assertEqual(3, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertIn("verification", proc.stderr)
        self.assertEqual([], self.adapter_calls())

    def test_sync_apply_absent_verification_root_projects_valid_intermediate_state(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "tracking_projection": "trello:publyapp",
                "local_review_ready_commands": ["exit 7"],
            }
        )
        failed = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, failed.returncode)
        # A FAILED verification leaves no artifact, so the verification root is
        # genuinely absent while the durable commit event remains: LOCAL_VERIFY
        # is a valid intermediate state and stays projectable (T6L-F6).
        self.assertFalse(
            os.path.exists(os.path.join(self.state_dir, PROJECT, str(PR), "verification"))
        )
        proc = self.run_cli(
            "sync", "--config", config, "--pr", str(PR), "--apply",
            "--projection-adapter", os.path.join(self.bin_dir, "adapter"),
            extra_env=self.adapter_env(),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state=LOCAL_VERIFY", proc.stdout)
        self.assertEqual(1, len(self.adapter_calls()))

    def _plant_verification_parent(self, kind):
        target = os.path.join(self.state_dir, PROJECT, str(PR), "verification")
        if os.path.lexists(target):
            os.unlink(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if kind == "regular-file":
            with open(target, "w") as handle:
                handle.write("not a directory")
        elif kind == "dangling-symlink":
            os.symlink(os.path.join(self.root, "no-such-verification-target"), target)
        elif kind == "external-directory-symlink":
            real = os.path.join(self.root, "external-verification-directory")
            os.makedirs(real, exist_ok=True)
            os.symlink(real, target)
        elif kind == "external-file-symlink":
            real = os.path.join(self.root, "external-verification-file")
            with open(real, "w") as handle:
                handle.write("x")
            os.symlink(real, target)
        elif kind == "fifo":
            os.mkfifo(target)
        else:
            raise AssertionError("unknown parent kind {0!r}".format(kind))
        return target

    def test_sync_apply_refuses_malformed_verification_parent_with_zero_adapter_calls(self):
        for kind in (
            "regular-file",
            "dangling-symlink",
            "external-directory-symlink",
            "external-file-symlink",
            "fifo",
        ):
            with self.subTest(parent=kind):
                config, args = self.prepare_projection()
                args.append("--apply")
                self._plant_verification_parent(kind)
                proc = self.run_cli(*args, extra_env=self.adapter_env())
                self.assertEqual(3, proc.returncode)
                self.assertEqual("", proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertIn("verification", proc.stderr)
                self.assertEqual([], self.adapter_calls())


class AdapterBoundaryTests(CliTestCase):
    """T6LC-F7 end-to-end: the projection adapter stream and diagnostic
    boundary is byte-bounded, typed, and never echoes adapter text."""

    def test_stdout_byte_cap_counts_bytes_not_characters(self):
        config, args = self.prepare_projection()
        raw = os.path.join(self.root, "adapter-wide-utf8.bin")
        with open(raw, "w", encoding="utf-8") as handle:
            handle.write("\u00e9" * 40000)
        proc = self.run_cli(*args, extra_env=self.adapter_env(raw_file=raw))
        self.assertEqual(5, proc.returncode)
        self.assertIn("exceeded the bounded stream limit", proc.stderr)
        self.assertNotIn("malformed JSON", proc.stderr)
        self.assertEqual(1, len(self.adapter_calls()))

    def test_invalid_utf8_stdout_is_typed_failure_without_traceback(self):
        config, args = self.prepare_projection()
        raw = os.path.join(self.root, "adapter-invalid-utf8.bin")
        with open(raw, "wb") as handle:
            handle.write(
                b'{"schema_version": 1, "applied": false, "changes": [], "x": "\xff\xfe"}'
            )
        proc = self.run_cli(*args, extra_env=self.adapter_env(raw_file=raw))
        self.assertEqual(5, proc.returncode)
        self.assertIn("invalid UTF-8", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("xff", proc.stderr)
        self.assertEqual(1, len(self.adapter_calls()))

    def test_nonzero_adapter_stderr_is_never_echoed(self):
        config, args = self.prepare_projection()
        args.append("--apply")
        secret = "SUPERSECRET-ADAPTER-BARE-SECRET"
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(exit_code=9, stderr=secret),
        )
        self.assertEqual(5, proc.returncode)
        self.assertNotIn(secret, proc.stderr)
        self.assertIn("exited with status 9", proc.stderr)
        self.assertEqual(1, len(self.adapter_calls()))

    def test_unknown_result_key_never_appears_in_diagnostics(self):
        config, args = self.prepare_projection()
        secret_key = "super_secret_result_key"
        secret_value = "super_secret_result_value"
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(
                output={
                    "schema_version": 1,
                    "applied": False,
                    "changes": [],
                    secret_key: secret_value,
                }
            ),
        )
        self.assertEqual(5, proc.returncode)
        self.assertNotIn(secret_key, proc.stderr)
        self.assertNotIn(secret_value, proc.stderr)
        self.assertEqual(1, len(self.adapter_calls()))

    def test_unknown_change_type_and_key_never_appear_in_diagnostics(self):
        config, args = self.prepare_projection()
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(
                output={
                    "schema_version": 1,
                    "applied": False,
                    "changes": [
                        {"type": "super_secret_type", "summary": "super secret summary"}
                    ],
                }
            ),
        )
        self.assertEqual(5, proc.returncode)
        self.assertNotIn("super_secret_type", proc.stderr)
        self.assertNotIn("super secret summary", proc.stderr)
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(
                output={
                    "schema_version": 1,
                    "applied": False,
                    "changes": [
                        {
                            "type": "card_update",
                            "summary": "ok",
                            "super_secret_change_key": "x",
                        }
                    ],
                }
            ),
        )
        self.assertEqual(5, proc.returncode)
        self.assertNotIn("super_secret_change_key", proc.stderr)
        self.assertEqual(2, len(self.adapter_calls()))

    def test_unhashable_change_type_is_typed_failure_without_traceback_or_leak(self):
        config, args = self.prepare_projection()
        cases = (
            (
                "list",
                {"type": ["super_secret_list_key", "super_secret_list_value"], "summary": "x"},
                ("super_secret_list_key", "super_secret_list_value"),
            ),
            (
                "object",
                {"type": {"super_secret_object_key": "super_secret_object_value"}, "summary": "x"},
                ("super_secret_object_key", "super_secret_object_value"),
            ),
        )
        for label, change, needles in cases:
            with self.subTest(shape=label):
                proc = self.run_cli(
                    *args,
                    extra_env=self.adapter_env(
                        output={
                            "schema_version": 1,
                            "applied": False,
                            "changes": [change],
                        }
                    ),
                )
                self.assertEqual(5, proc.returncode)
                self.assertIn("unknown type", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                for needle in needles:
                    self.assertNotIn(needle, proc.stderr)
        self.assertEqual(len(cases), len(self.adapter_calls()))


class AdapterProcessBoundaryTests(CliTestCase):
    """T6LC-F7 in-process: capture honors one strict deadline over parent
    execution and pipe EOF, the whole process group is terminated, and no
    live descendant survives timeout or overflow."""

    def _run_adapter(self, script, timeout):
        outcome = {}

        def _worker():
            try:
                outcome["value"] = _invoke_adapter([script], timeout=timeout)
                outcome["raised"] = None
            except BaseException as error:  # noqa: BLE001 - typed assertion surface
                outcome["raised"] = error

        worker = threading.Thread(target=_worker, daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(timeout=8.0)
        outcome["elapsed"] = time.monotonic() - started
        if worker.is_alive():
            outcome["raised"] = AssertionError(
                "_invoke_adapter did not return within the outer deadline"
            )
        return outcome

    def _assert_descendant_not_live(self, pidfile):
        with open(pidfile) as handle:
            pid = int(handle.read().strip())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not self._is_live(pid):
                return
            time.sleep(0.05)
        self.fail("descendant pid {0} is still live after cleanup".format(pid))

    def _is_live(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            with open("/proc/{0}/stat".format(pid)) as handle:
                state = handle.read().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state not in ("Z", "X")

    def test_parent_exit_with_pipe_holding_descendant_times_out_bounded_and_reaps_group(self):
        pidfile = os.path.join(self.root, "holder.pid")
        script = self._write_fake("holder-adapter", _PIPE_HOLDER_SCRIPT)
        with mock.patch.dict(os.environ, {"ADAPTER_PIDFILE": pidfile}):
            outcome = self._run_adapter(script, timeout=1.0)
        self.assertIsInstance(outcome["raised"], ProjectionFailure)
        self.assertIn("timed out", str(outcome["raised"]))
        self.assertLess(outcome["elapsed"], 5.0)
        self._assert_descendant_not_live(pidfile)

    def test_stdout_overflow_leaves_no_live_descendant(self):
        pidfile = os.path.join(self.root, "overflow.pid")
        script = self._write_fake("overflow-adapter", _STDOUT_OVERFLOW_SCRIPT)
        with mock.patch.dict(os.environ, {"ADAPTER_PIDFILE": pidfile}):
            outcome = self._run_adapter(script, timeout=5.0)
        self.assertIsInstance(outcome["raised"], ProjectionFailure)
        self.assertIn("exceeded the bounded stream limit", str(outcome["raised"]))
        self.assertLess(outcome["elapsed"], 5.0)
        self._assert_descendant_not_live(pidfile)

    def test_stderr_overflow_leaves_no_live_descendant(self):
        pidfile = os.path.join(self.root, "stderr-overflow.pid")
        script = self._write_fake("stderr-overflow-adapter", _STDERR_OVERFLOW_SCRIPT)
        with mock.patch.dict(os.environ, {"ADAPTER_PIDFILE": pidfile}):
            outcome = self._run_adapter(script, timeout=5.0)
        self.assertIsInstance(outcome["raised"], ProjectionFailure)
        self.assertIn("exceeded the bounded stream limit", str(outcome["raised"]))
        self.assertLess(outcome["elapsed"], 5.0)
        self._assert_descendant_not_live(pidfile)


class SourceFailureRedactionTests(CliTestCase):
    SECRETS = (
        "ghp_SUPERSECRET",
        "Authorization: Bearer ghp_BEARERSECRET",
        "GH_TOKEN: ghp_WHITESPACESECRET",
        "supersecret",
        "ab12",
    )

    def assert_secrets_absent(self, proc):
        for secret in self.SECRETS:
            self.assertNotIn(secret, proc.stderr)
        self.assertEqual("", proc.stdout)

    def test_gh_failure_diagnostics_are_redacted(self):
        self.set_git()
        stderr = (
            "HTTP 401: Authorization: Bearer ghp_BEARERSECRET rejected; "
            "GH_TOKEN: ghp_WHITESPACESECRET invalid (token=ghp_SUPERSECRET); "
            "token: supersecret and --token ab12 also leak"
        )
        self.set_gh_failure(stderr)
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assert_secrets_absent(proc)

    def test_git_failure_diagnostics_are_redacted(self):
        self.set_git_failure(
            "worktree list --porcelain",
            "fatal: denied for https://user:ghp_URLSECRET@github.com/owner/repo",
        )
        self.set_gh()
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertNotIn("ghp_URLSECRET", proc.stderr)
        self.assertEqual("", proc.stdout)

    def test_status_json_error_keeps_stdout_machine_readable(self):
        self.set_git()
        self.set_gh_failure("HTTP 401: Authorization: Bearer ghp_BEARERSECRET rejected")
        config = self.write_config()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR), "--json")
        self.assertEqual(3, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertNotIn("ghp_BEARERSECRET", proc.stderr)

    def test_verification_failure_stderr_is_redacted(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["echo 'token: supersecret rejected'; exit 7"],
                "closure_acceptance_commands": ["true"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        self.assertNotIn("supersecret", proc.stderr)
        self.assertNotIn("ab12", proc.stderr)

    def test_projection_adapter_stderr_is_redacted(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(overrides={"tracking_projection": "trello:publyapp"})
        proc = self.run_cli(
            "sync", "--config", config, "--pr", str(PR), "--apply",
            "--projection-adapter", os.path.join(self.bin_dir, "adapter"),
            extra_env=self.adapter_env(exit_code=1, stderr="boom token: supersecret --token ab12"),
        )
        self.assertEqual(5, proc.returncode)
        self.assertNotIn("supersecret", proc.stderr)
        self.assertNotIn("ab12", proc.stderr)


class AdapterInvocationUnitTests(unittest.TestCase):
    """Timeout and malformed-result handling for the projection adapter seam."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pctest-", dir=DURABLE_TMP)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.bin_dir = os.path.join(self.root, "bin")
        os.makedirs(self.bin_dir)
        path = os.path.join(self.bin_dir, "sleep-adapter")
        with open(path, "w") as handle:
            handle.write("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
        os.chmod(path, stat.S_IRWXU)
        self.path = path

    def test_adapter_timeout_fails_closed(self):
        from pr_closure.cli import ProjectionFailure, _invoke_adapter

        with self.assertRaises(ProjectionFailure):
            _invoke_adapter((self.path, "--mode", "dry-run"), timeout=0.2)

    def test_adapter_oserror_fails_closed(self):
        from pr_closure.cli import ProjectionFailure, _invoke_adapter

        with self.assertRaises(ProjectionFailure):
            _invoke_adapter((os.path.join(self.root, "missing-adapter"), "--mode", "dry-run"), timeout=5)

    def test_adapter_result_parsing_pins_protocol(self):
        from pr_closure.cli import ProjectionFailure, _parse_adapter_result

        good = _parse_adapter_result(
            '{"schema_version": 1, "applied": false, "changes": []}', "dry-run"
        )
        self.assertFalse(good["applied"])
        for payload, mode in (
            ("not json", "dry-run"),
            ("[]", "dry-run"),
            ('{"schema_version": 2, "applied": false, "changes": []}', "dry-run"),
            ('{"applied": false, "changes": []}', "dry-run"),
            ('{"schema_version": 1, "applied": "no", "changes": []}', "dry-run"),
            ('{"schema_version": 1, "applied": true, "changes": []}', "dry-run"),
            ('{"schema_version": 1, "applied": false}', "dry-run"),
            ('{"schema_version": 1, "applied": false, "changes": {}}', "dry-run"),
            ('{"schema_version": 1, "applied": false, "changes": []}', "apply"),
        ):
            with self.subTest(payload=payload, mode=mode):
                with self.assertRaises(ProjectionFailure):
                    _parse_adapter_result(payload, mode)


class ConfigValidationTests(CliTestCase):
    def assert_config_rejected(self, overrides, fragment, command="status"):
        self.set_git()
        self.set_gh()
        config = self.write_config(overrides=overrides)
        proc = self.run_cli(command, "--config", config, "--pr", str(PR))
        self.assertEqual(2, proc.returncode)
        self.assertIn(fragment, proc.stderr)
        return proc

    def test_rejects_unknown_keys(self):
        self.assert_config_rejected({"bogus_key": 1}, "bogus_key")

    def test_rejects_missing_keys(self):
        for key in (
            "schema_version", "project", "repository", "repo_path", "default_branch",
            "closure_state_dir", "local_review_ready_commands", "closure_acceptance_commands",
            "infra_retry_budget", "stagnation_budget_minutes", "heavy_job_limit",
            "verification_command_timeout_seconds",
            "tracking_projection",
        ):
            with self.subTest(key=key):
                config = dict(BASE_CONFIG)
                config["repo_path"] = self.repo_path
                config["closure_state_dir"] = self.state_dir
                del config[key]
                path = os.path.join(self.root, "closure.json")
                self._write_json(path, config)
                self.set_git()
                self.set_gh()
                proc = self.run_cli("status", "--config", path, "--pr", str(PR))
                self.assertEqual(2, proc.returncode, proc.stderr)
                self.assertIn(key, proc.stderr)

    def test_rejects_unsupported_schema_version(self):
        self.assert_config_rejected({"schema_version": 2}, "schema_version")

    def test_rejects_boolean_and_float_integer_fields(self):
        for field in (
            "infra_retry_budget",
            "stagnation_budget_minutes",
            "heavy_job_limit",
            "verification_command_timeout_seconds",
        ):
            for bad in (True, 1.5):
                with self.subTest(field=field, value=bad):
                    self.assert_config_rejected({field: bad}, field)

    def test_rejects_non_positive_budgets(self):
        for field in ("infra_retry_budget", "stagnation_budget_minutes", "verification_command_timeout_seconds"):
            for bad in (0, -1):
                with self.subTest(field=field, value=bad):
                    self.assert_config_rejected({field: bad}, field)

    def test_rejects_heavy_job_limit_other_than_exact_one(self):
        for bad in (0, 2, 3):
            with self.subTest(value=bad):
                self.assert_config_rejected({"heavy_job_limit": bad}, "heavy_job_limit")

    def test_rejects_unsafe_project_names(self):
        for bad in (".", "..", "a/b", "a\\b", " spaced ", "a\x00b"):
            with self.subTest(value=bad):
                self.assert_config_rejected({"project": bad}, "project")

    def test_rejects_unsafe_repository(self):
        for bad in ("norepo", "/repo", "owner/", "owner repo/x"):
            with self.subTest(value=bad):
                self.assert_config_rejected({"repository": bad}, "repository")

    def test_rejects_unsafe_default_branch(self):
        for bad in ("", "  ", "branch name"):
            with self.subTest(value=bad):
                self.assert_config_rejected({"default_branch": bad}, "default_branch")

    def test_rejects_relative_repo_path(self):
        self.assert_config_rejected({"repo_path": "relative/path"}, "repo_path")

    def test_rejects_forbidden_temporary_paths(self):
        self.assert_config_rejected({"repo_path": "/tmp/scratch/repo"}, "repo_path")
        self.assert_config_rejected({"closure_state_dir": "/tmp/scratch/state"}, "closure_state_dir")

    def test_rejects_empty_or_whitespace_command_lists(self):
        for field in ("local_review_ready_commands", "closure_acceptance_commands"):
            for bad in ([], ["   "], [""], [42], ["ok", None]):
                with self.subTest(field=field, value=bad):
                    self.assert_config_rejected({field: bad}, field)

    def test_rejects_bad_tracking_projection(self):
        for bad in ("", "   ", 42, []):
            with self.subTest(value=bad):
                self.assert_config_rejected({"tracking_projection": bad}, "tracking_projection")

    def test_rejects_blank_or_duplicate_ci_required_checks(self):
        for bad, fragment in (
            (["gate-a", "  "], "ci_required_checks"),
            (["gate-a", "gate-a"], "ci_required_checks"),
            ([42], "ci_required_checks"),
            ("gate-a", "ci_required_checks"),
        ):
            with self.subTest(value=bad):
                self.assert_config_rejected({"ci_required_checks": bad}, fragment)

    def test_accepts_empty_or_declared_ci_required_checks(self):
        self.set_git()
        self.set_gh()
        for value in ([], ["gate-a", "gate-b"]):
            with self.subTest(value=value):
                config = self.write_config(overrides={"ci_required_checks": value})
                proc = self.run_cli("status", "--config", config, "--pr", str(PR))
                self.assertEqual(0, proc.returncode, proc.stderr)

    def test_accepts_tracking_projection_string_or_null(self):
        self.set_git()
        self.set_gh()
        config = self.write_config(overrides={"tracking_projection": "trello card update"})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        config = self.write_config(overrides={"tracking_projection": None})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_rejects_non_json_config_file(self):
        self.set_git()
        self.set_gh()
        path = os.path.join(self.root, "bad.json")
        with open(path, "w") as handle:
            handle.write("not json{")
        proc = self.run_cli("status", "--config", path, "--pr", str(PR))
        self.assertEqual(2, proc.returncode)


class ShellBoundaryTests(CliTestCase):
    def test_config_path_with_spaces_never_reaches_shell(self):
        spaced = os.path.join(self.root, "dir with spaces")
        os.makedirs(spaced)
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        marker = os.path.join(spaced, "marker file.txt")
        command = "printf '%s\\n' 'quoted value' > " + shlex.quote(marker)
        config = self.write_config(
            path=os.path.join(spaced, "closure config.json"),
            overrides={"local_review_ready_commands": [command]},
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        with open(marker) as handle:
            self.assertEqual("quoted value\n", handle.read())

    def test_pr_argument_never_reaches_shell(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        command = "printf '%s' '$(id -u)' > " + self.marker
        config = self.write_config(overrides={"local_review_ready_commands": [command]})
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        with open(self.marker) as handle:
            content = handle.read()
        self.assertNotIn(str(PR), content)
        self.assertNotIn(REPOSITORY, content)


class ProjectionRedactionTests(CliTestCase):
    def test_sync_dry_run_never_displays_change_summaries(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(overrides={"tracking_projection": "trello:publyapp"})
        output = {
            "schema_version": 1,
            "applied": False,
            "changes": [
                {
                    "type": "list_update",
                    "summary": "update card GH_TOKEN: ghp_PROJSECRET; --token ab12",
                }
            ],
        }
        proc = self.run_cli(
            "sync", "--config", config, "--pr", str(PR),
            "--projection-adapter", os.path.join(self.bin_dir, "adapter"),
            extra_env=self.adapter_env(output=output),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("projection dry-run: changes=1", proc.stdout)
        self.assertNotIn("ghp_PROJSECRET", proc.stdout)
        self.assertNotIn("ab12", proc.stdout)
        self.assertNotIn("ghp_PROJSECRET", proc.stderr)



class GateWorktreeBindingTests(CliTestCase):
    """C6D-F1: gates run in the resolved PR worktree and the binding is
    revalidated before evidence is committed."""

    def test_gate_commands_run_in_the_resolved_pr_worktree(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        marker = os.path.join(self.root, "cwd-marker")
        command = "pwd > " + shlex.quote(marker)
        config = self.write_config(overrides={"local_review_ready_commands": [command]})
        proc = self.run_cli(
            "record-verification", "--config", config, "--pr", str(PR), cwd=self.root
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        with open(marker) as handle:
            observed = handle.read().strip()
        self.assertEqual(os.path.realpath(self.pr_worktree), os.path.realpath(observed))
        self.assertNotEqual(os.path.realpath(self.root), os.path.realpath(observed))
        self.assertNotEqual(os.path.realpath(self.repo_path), os.path.realpath(observed))
        with open(self.verification_path(COMMIT_A)) as handle:
            record = json.load(handle)
        self.assertEqual("PASSED", record["outcome"])

    def test_gate_binding_revalidated_before_evidence_commit(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        mutate = (
            "python3 -c \"import json;"
            "p=json.load(open('{control}'));"
            "p['rev_parse_head']='{other}';"
            "json.dump(p, open('{control}','w'))\""
        ).format(control=self.git_control, other=COMMIT_B)
        config = self.write_config(overrides={"local_review_ready_commands": [mutate]})
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertFalse(os.path.exists(self.verification_path(COMMIT_A)))
        self.assertNotIn("PASSED", self.read_events_raw())


class ConfigCommandBindingTests(CliTestCase):
    """C6D-F2: status authority requires the exact current config sequence."""

    def test_status_approves_only_the_exact_configured_sequence(self):
        config = self.prepare_approved()
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED", proc.stdout)

    def test_changed_config_command_makes_old_evidence_non_green(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        review = self.write_review(commit=COMMIT_A)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED", proc.stdout)
        changed = self.write_config(
            overrides={"closure_acceptance_commands": ["echo changed"]}
        )
        proc = self.run_cli("status", "--config", changed, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("APPROVED", proc.stdout.split("state: ")[1].splitlines()[0])
        proc = self.run_cli(
            "check-transition", "--config", changed, "--pr", str(PR), "--to", "APPROVED"
        )
        self.assertEqual(4, proc.returncode)

    def test_changed_phase_array_makes_old_evidence_non_green(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["true", "true"],
                "closure_acceptance_commands": ["true"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        review = self.write_review(commit=COMMIT_A)
        proc = self.run_cli("import-review", "--config", config, "--pr", str(PR), "--review", review)
        self.assertEqual(0, proc.returncode, proc.stderr)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: APPROVED", proc.stdout)
        moved = self.write_config(
            overrides={
                "local_review_ready_commands": ["true"],
                "closure_acceptance_commands": ["true", "true"],
            }
        )
        proc = self.run_cli("status", "--config", moved, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("APPROVED", proc.stdout.split("state: ")[1].splitlines()[0])


class SecretCommandNeverPersistsTests(CliTestCase):
    """C6D-F4: raw configured command secrets never reach durable evidence."""

    SECRETS = ("hunter2", "sesame", "opensesame")

    def _state_bytes(self):
        chunks = []
        for dirpath, dirnames, filenames in os.walk(self.state_dir):
            for name in filenames:
                path = os.path.join(dirpath, name)
                with open(path, "rb") as handle:
                    chunks.append(handle.read())
        return b"".join(chunks)

    def test_passed_evidence_never_persists_raw_command_secrets(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["echo hunter2"],
                "closure_acceptance_commands": ["echo sesame"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        blob = self._state_bytes()
        for secret in self.SECRETS:
            self.assertNotIn(secret.encode(), blob)
        with open(self.verification_path(COMMIT_A)) as handle:
            record = json.load(handle)
        self.assertEqual(hashlib.sha256(b"echo hunter2").hexdigest(), record["commands"][0]["command_digest"])
        self.assertEqual(hashlib.sha256(b"echo sesame").hexdigest(), record["commands"][1]["command_digest"])
        self.assertEqual("local_review_ready:1", record["commands"][0]["command_label"])
        self.assertEqual(0, record["commands"][0]["exit_status"])

    def test_failed_evidence_never_persists_raw_command_secrets(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["echo opensesame; exit 9"],
                "closure_acceptance_commands": ["echo sesame"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        blob = self._state_bytes()
        for secret in self.SECRETS:
            self.assertNotIn(secret.encode(), blob)
        failed = [e for e in self.read_events() if e["event_type"] == "verification"]
        self.assertEqual(1, len(failed))
        self.assertEqual(
            hashlib.sha256(b"echo opensesame; exit 9").hexdigest(),
            failed[0]["failed_command_digest"],
        )

    def test_lease_metadata_never_persists_raw_command_secrets(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        meta_marker = os.path.join(self.root, "meta-copy.json")
        meta_path = os.path.join(self.state_dir, PROJECT, str(PR), "heavy-job.meta.json")
        config = self.write_config(
            overrides={
                "local_review_ready_commands": [
                    "cat " + shlex.quote(meta_path) + " > " + shlex.quote(meta_marker)
                ],
                "closure_acceptance_commands": ["echo hunter2"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        with open(meta_marker) as handle:
            meta = handle.read()
        self.assertNotIn("hunter2", meta)
        self.assertNotIn("sesame", meta)
        self.assertNotIn("echo", meta)
        blob = self._state_bytes()
        self.assertNotIn(b"hunter2", blob)
        self.assertNotIn(b"sesame", blob)


class SameTipReverificationCliTests(CliTestCase):
    """T6L-F2 end-to-end: attempts and config identities at one commit coexist
    without requiring an unrelated or no-op commit."""

    def verification_paths(self, commit=COMMIT_A):
        base = os.path.join(self.state_dir, PROJECT, str(PR), "verification", commit)
        if not os.path.isdir(base):
            return []
        return sorted(
            os.path.join(dirpath, name)
            for dirpath, dirnames, filenames in os.walk(base)
            for name in filenames
        )

    def artifact_bytes(self, commit=COMMIT_A):
        return {
            path: Path(path).read_bytes()
            for path in self.verification_paths(commit)
        }

    def read_verification_events(self):
        return [e for e in self.read_events() if e["event_type"] == "verification"]

    def test_first_pass_records_one_attempt_at_the_commit(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        paths = self.verification_paths()
        self.assertEqual(1, len(paths))
        record = json.loads(Path(paths[0]).read_text())
        self.assertEqual(COMMIT_A, record["commit"])
        self.assertEqual("PASSED", record["outcome"])
        self.assertIn(record["config_digest"], paths[0])
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: REVIEW_READY", proc.stdout)

    def test_same_config_rerun_coexists_and_stays_green(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        first = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, first.returncode, first.stderr)
        original = self.artifact_bytes()
        second = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(2, len(self.verification_paths()))
        self.assertEqual(2, len(self.read_verification_events()))
        for path, bytes_ in original.items():
            self.assertEqual(bytes_, Path(path).read_bytes())
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: REVIEW_READY", proc.stdout)

    def test_changed_config_rerun_needs_no_unrelated_commit(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config_a = self.write_config()
        first = self.run_cli("record-verification", "--config", config_a, "--pr", str(PR))
        self.assertEqual(0, first.returncode, first.stderr)
        original = self.artifact_bytes()
        self.assertEqual(1, len(original))
        config_b = self.write_config(
            overrides={"closure_acceptance_commands": ["true # stricter"]}
        )
        second = self.run_cli("record-verification", "--config", config_b, "--pr", str(PR))
        self.assertEqual(0, second.returncode, second.stderr)
        paths = self.verification_paths()
        self.assertEqual(2, len(paths))
        digests = {
            json.loads(Path(path).read_text())["config_digest"] for path in paths
        }
        self.assertEqual(2, len(digests))
        for path, bytes_ in original.items():
            self.assertEqual(bytes_, Path(path).read_bytes(), path)
        # New config is green; old config evidence remains selectable as well.
        proc = self.run_cli("status", "--config", config_b, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: REVIEW_READY", proc.stdout)
        proc = self.run_cli("status", "--config", config_a, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: REVIEW_READY", proc.stdout)

    def test_timeout_variation_changes_verification_identity(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config_short = self.write_config(
            overrides={"verification_command_timeout_seconds": 1}
        )
        short = self.run_cli(
            "record-verification", "--config", config_short, "--pr", str(PR)
        )
        self.assertEqual(0, short.returncode, short.stderr)
        config_long = self.write_config(
            overrides={"verification_command_timeout_seconds": 2}
        )
        before = self.run_cli(
            "status", "--config", config_long, "--pr", str(PR), "--json"
        )
        self.assertEqual(0, before.returncode, before.stderr)
        self.assertFalse(json.loads(before.stdout)["local_verification"])
        long_pass = self.run_cli(
            "record-verification", "--config", config_long, "--pr", str(PR)
        )
        self.assertEqual(0, long_pass.returncode, long_pass.stderr)
        after = self.run_cli("status", "--config", config_long, "--pr", str(PR), "--json")
        self.assertEqual(0, after.returncode, after.stderr)
        self.assertTrue(json.loads(after.stdout)["local_verification"])
        paths = self.verification_paths()
        self.assertEqual(2, len(paths))
        digests = {
            json.loads(Path(path).read_text())["config_digest"] for path in paths
        }
        self.assertEqual(2, len(digests))

    def test_failed_then_pass_keeps_failed_event_and_recovers(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        command = "test -e " + self.marker + " && exit 0; touch " + self.marker + "; exit 1"
        config = self.write_config(overrides={"local_review_ready_commands": [command]})
        failed = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, failed.returncode)
        self.assertEqual(0, len(self.verification_paths()))
        self.assertEqual(1, len(self.read_verification_events()))
        self.assertEqual("FAILED", self.read_verification_events()[0]["outcome"])
        passed = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, passed.returncode, passed.stderr)
        self.assertEqual(1, len(self.verification_paths()))
        events = self.read_verification_events()
        self.assertIn("FAILED", {e.get("outcome") for e in events})
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: REVIEW_READY", proc.stdout)

    def test_pass_then_fail_never_erases_the_valid_pass(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        command = "test ! -e " + self.marker + " && touch " + self.marker
        config = self.write_config(overrides={"local_review_ready_commands": [command]})
        passed = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, passed.returncode, passed.stderr)
        original = self.artifact_bytes()
        self.assertEqual(1, len(original))
        failed = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, failed.returncode)
        events = self.read_verification_events()
        self.assertIn("FAILED", {e.get("outcome") for e in events})
        for path, bytes_ in original.items():
            self.assertEqual(bytes_, Path(path).read_bytes(), path)
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("state: REVIEW_READY", proc.stdout)

    def test_two_config_identities_at_one_commit_both_selectable(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config_a = self.write_config()
        first = self.run_cli("record-verification", "--config", config_a, "--pr", str(PR))
        self.assertEqual(0, first.returncode, first.stderr)
        config_b = self.write_config(
            overrides={"local_review_ready_commands": ["true # variant"]}
        )
        second = self.run_cli("record-verification", "--config", config_b, "--pr", str(PR))
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(2, len(self.verification_paths()))
        for config in (config_a, config_b):
            proc = self.run_cli("status", "--config", config, "--pr", str(PR))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertIn("state: REVIEW_READY", proc.stdout)


class LiveReviewBindingTests(CliTestCase):
    """T6L-F3: every durable review consumed by status is re-bound to the live
    repository, PR number, head branch, and head commit on every read."""

    def _foreign_record(self, **overrides):
        record = {
            "schema_version": 1,
            "repository": REPOSITORY,
            "pr_number": PR,
            "reviewed_branch": BRANCH,
            "reviewed_commit": COMMIT_A,
            "base_commit": "b" * 40,
            "implementer_family": "deepseek",
            "reviewer_family": "claude",
            "local_evidence": ["tests:tools/tests/test_cli.py"],
            "ci_evidence": ["ci:pr-check/run-1"],
            "verdict": "APPROVED",
            "findings": [],
            "intentionally_not_findings": [],
        }
        record.update(overrides)
        return record

    def test_foreign_repository_fails_closed_on_read(self):
        config = self.prepare_approved()
        store = RunStore(self.state_dir, PROJECT, PR)
        store.write_review(COMMIT_A, "foreign", self._foreign_record(repository="evil/repo"))
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("repository", proc.stderr)

    def test_foreign_pr_number_fails_closed_on_read(self):
        config = self.prepare_approved()
        store = RunStore(self.state_dir, PROJECT, PR)
        store.write_review(COMMIT_A, "foreign", self._foreign_record(pr_number=999))
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("pr_number", proc.stderr)

    def test_foreign_head_branch_fails_closed_on_read(self):
        config = self.prepare_approved()
        store = RunStore(self.state_dir, PROJECT, PR)
        store.write_review(COMMIT_A, "foreign", self._foreign_record(reviewed_branch="evil/branch"))
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("reviewed_branch", proc.stderr)

    def test_foreign_head_commit_fails_closed_on_read(self):
        self.set_git(local=COMMIT_B, remote=COMMIT_B)
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config()
        store = RunStore(self.state_dir, PROJECT, PR)
        store.record_commit(COMMIT_B, str(store.events_path))
        store.write_review(COMMIT_B, "foreign", self._foreign_record(reviewed_commit=COMMIT_B))
        proc = self.run_cli("status", "--config", config, "--pr", str(PR))
        self.assertEqual(3, proc.returncode)
        self.assertIn("reviewed_commit", proc.stderr)


class IntermediateStateProjectionTests(CliTestCase):
    """Preserved non-finding: sync --apply must project valid intermediate
    states (e.g. LOCAL_VERIFY); it refuses adapter invocation only when a
    required source is unavailable, malformed, or contradictory."""

    def test_valid_intermediate_state_can_be_applied(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        store = RunStore(self.state_dir, PROJECT, PR)
        store.record_commit(COMMIT_A, str(store.events_path))
        config = self.write_config(overrides={"tracking_projection": "trello:publyapp"})
        proc = self.run_cli(
            "sync", "--config", config, "--pr", str(PR), "--apply",
            "--projection-adapter", os.path.join(self.bin_dir, "adapter"),
            extra_env=self.adapter_env(),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        calls = self.adapter_calls()
        self.assertEqual(1, len(calls))
        argv = calls[0]
        self.assertEqual("LOCAL_VERIFY", argv[argv.index("--state") + 1])
        self.assertEqual("apply", argv[argv.index("--mode") + 1])
        self.assertIn("state=LOCAL_VERIFY", proc.stdout)


class ProjectionResultContractTests(CliTestCase):
    """T6L-F7: version-1 projection results are strict and bounded."""

    def test_adapter_result_rejects_unknown_keys(self):
        from pr_closure.cli import ProjectionFailure, _parse_adapter_result

        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                '{"schema_version": 1, "applied": false, "changes": [], "secret": "leaked"}',
                "dry-run",
            )

    def test_adapter_result_rejects_scalar_change_elements(self):
        from pr_closure.cli import ProjectionFailure, _parse_adapter_result

        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                '{"schema_version": 1, "applied": false, "changes": ["x"]}',
                "dry-run",
            )

    def test_adapter_result_rejects_unknown_change_keys(self):
        from pr_closure.cli import ProjectionFailure, _parse_adapter_result

        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                '{"schema_version": 1, "applied": false, "changes": ['
                '{"type": "list_update", "summary": "ok", "rogue": 1}]}',
                "dry-run",
            )

    def test_adapter_result_rejects_unknown_change_type(self):
        from pr_closure.cli import ProjectionFailure, _parse_adapter_result

        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                '{"schema_version": 1, "applied": false, "changes": '
                '[{"type": "bogus", "summary": "x"}]}',
                "dry-run",
            )

    def test_adapter_result_rejects_list_change_type(self):
        from pr_closure.cli import ProjectionFailure, _parse_adapter_result

        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                '{"schema_version": 1, "applied": false, "changes": '
                '[{"type": ["secret", "value"], "summary": "x"}]}',
                "dry-run",
            )

    def test_adapter_result_rejects_object_change_type(self):
        from pr_closure.cli import ProjectionFailure, _parse_adapter_result

        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                '{"schema_version": 1, "applied": false, "changes": '
                '[{"type": {"secret": "value"}, "summary": "x"}]}',
                "dry-run",
            )

    def test_adapter_result_rejects_excessive_change_count(self):
        from pr_closure.cli import PROJECTION_MAX_CHANGES, ProjectionFailure, _parse_adapter_result

        changes = [
            {"type": "list_update", "summary": "c{0}".format(i)}
            for i in range(PROJECTION_MAX_CHANGES + 1)
        ]
        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                json.dumps({"schema_version": 1, "applied": False, "changes": changes}),
                "dry-run",
            )

    def test_adapter_result_rejects_excessive_summary_length(self):
        from pr_closure.cli import (
            PROJECTION_CHANGE_SUMMARY_MAX,
            ProjectionFailure,
            _parse_adapter_result,
        )

        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                json.dumps({
                    "schema_version": 1,
                    "applied": False,
                    "changes": [
                        {"type": "list_update", "summary": "s" * (PROJECTION_CHANGE_SUMMARY_MAX + 1)}
                    ],
                }),
                "dry-run",
            )

    def test_adapter_result_rejects_non_string_summary(self):
        from pr_closure.cli import ProjectionFailure, _parse_adapter_result

        with self.assertRaises(ProjectionFailure):
            _parse_adapter_result(
                '{"schema_version": 1, "applied": false, "changes": '
                '[{"type": "list_update", "summary": 42}]}',
                "dry-run",
            )

    def test_adapter_result_accepts_bounded_change_records(self):
        from pr_closure.cli import _parse_adapter_result

        data = _parse_adapter_result(
            '{"schema_version": 1, "applied": false, "changes": '
            '[{"type": "card_update", "summary": "moved card"}]}',
            "dry-run",
        )
        self.assertEqual(1, len(data["changes"]))


class ConfiguredCommandOutputNeverEmittedTests(CliTestCase):
    """T6L-F8: configured command stdout/stderr never reaches CLI streams or
    durable bytes, regardless of secret carriers."""

    SECRETS = (
        "bare-secret-xyz",
        "token all-alpha-secret-1",
        "password all-alpha-secret-2",
    )

    def _state_bytes(self):
        chunks = []
        for dirpath, dirnames, filenames in os.walk(self.state_dir):
            for name in filenames:
                path = os.path.join(dirpath, name)
                with open(path, "rb") as handle:
                    chunks.append(handle.read())
        return b"".join(chunks)

    def test_failing_commands_never_emit_output_to_any_stream(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": [
                    "printf '%s\n' 'token all-alpha-secret-1' >&2; "
                    "printf '%s\n' 'password all-alpha-secret-2' >&1; "
                    "printf '%s\n' 'bare-secret-xyz' >&2; exit 9"
                ],
                "closure_acceptance_commands": ["true"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        combined = proc.stdout + proc.stderr
        for secret in self.SECRETS:
            self.assertNotIn(secret, combined)
        blob = self._state_bytes()
        for secret in self.SECRETS:
            self.assertNotIn(secret.encode(), blob)
        failed = [e for e in self.read_events() if e["event_type"] == "verification"]
        self.assertEqual(1, len(failed))
        self.assertNotIn("bare-secret-xyz", json.dumps(failed[0]))

    def test_successful_commands_never_emit_output_to_any_stream(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": [
                    "printf '%s\n' 'token all-alpha-secret-1' >&2; "
                    "printf '%s\n' 'bare-secret-xyz'"
                ],
                "closure_acceptance_commands": [
                    "printf '%s\n' 'password all-alpha-secret-2' >&2"
                ],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(0, proc.returncode, proc.stderr)
        combined = proc.stdout + proc.stderr
        for secret in self.SECRETS:
            self.assertNotIn(secret, combined)
        blob = self._state_bytes()
        for secret in self.SECRETS:
            self.assertNotIn(secret.encode(), blob)

    def test_verification_failure_diagnostics_carry_only_safe_fields(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(
            overrides={
                "local_review_ready_commands": ["echo 'should-not-appear'; exit 7"],
                "closure_acceptance_commands": ["true"],
            }
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(5, proc.returncode)
        self.assertNotIn("should-not-appear", proc.stderr)
        self.assertIn("local_review_ready:1", proc.stderr)


class NulCommandConfigTests(CliTestCase):
    """T6L-F9: NUL command strings are rejected at config validation and the
    subprocess ValueError path is mapped to the typed CLI contract."""

    def test_nul_command_config_exits_two_without_traceback(self):
        self.set_git()
        self.set_gh()
        config = self.write_config(
            overrides={"local_review_ready_commands": ["echo \x00 boom"]}
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(2, proc.returncode)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("ValueError", proc.stderr)

    def test_nul_command_config_writes_no_state(self):
        self.set_git()
        self.set_gh()
        config = self.write_config(
            overrides={"closure_acceptance_commands": ["echo \x00 boom"]}
        )
        proc = self.run_cli("record-verification", "--config", config, "--pr", str(PR))
        self.assertEqual(2, proc.returncode)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_subprocess_valueerror_is_mapped_to_typed_failure(self):
        from pr_closure.cli import VerificationFailure, _run_shell_command

        with self.assertRaises(VerificationFailure):
            _run_shell_command("echo \x00 boom", cwd=self.pr_worktree, timeout_seconds=1)

    def test_run_shell_command_requires_explicit_timeout(self):
        from pr_closure.cli import _run_shell_command

        with self.assertRaises(TypeError):
            _run_shell_command("true", cwd=self.pr_worktree)


class AdapterBoundedOutputTests(CliTestCase):
    """T6L-F7: adapter stdout/stderr are bounded while reading; the process is
    terminated and reaped on overflow or timeout; secrets are never echoed."""

    def _write_adapter(self, name, body):
        path = os.path.join(self.bin_dir, name)
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, stat.S_IRWXU)
        return path

    def test_excessive_adapter_stdout_fails_closed(self):
        from pr_closure.cli import ProjectionFailure, _invoke_adapter

        adapter = self._write_adapter(
            "huge-out", "#!/usr/bin/env python3\nimport sys\n"
            "sys.stdout.write('x' * 2_000_000)\n"
        )
        with self.assertRaises(ProjectionFailure):
            _invoke_adapter((adapter, "--mode", "dry-run"), timeout=5)

    def test_excessive_adapter_stderr_on_failure_is_bounded_and_redacted(self):
        from pr_closure.cli import ProjectionFailure, _invoke_adapter

        adapter = self._write_adapter(
            "huge-err",
            "#!/usr/bin/env python3\nimport sys\n"
            "sys.stderr.write('token ab12 ' + 'z' * 2_000_000)\n"
            "sys.exit(9)\n",
        )
        with self.assertRaises(ProjectionFailure):
            _invoke_adapter((adapter, "--mode", "apply"), timeout=5)

    def test_timeout_process_is_reaped(self):
        from pr_closure.cli import ProjectionFailure, _invoke_adapter

        pid_marker = os.path.join(self.root, "adapter.pid")
        adapter = self._write_adapter(
            "pid-sleeper",
            "#!/usr/bin/env python3\nimport os, time\n"
            "open({0!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n".format(pid_marker),
        )
        with self.assertRaises(ProjectionFailure):
            _invoke_adapter((adapter, "--mode", "dry-run"), timeout=0.2)
        with open(pid_marker) as handle:
            pid = int(handle.read().strip())
        import errno

        try:
            os.waitpid(pid, os.WNOHANG)
            reaped = False
        except ChildProcessError:
            reaped = True
        self.assertTrue(reaped, "adapter process was not reaped after timeout")

    def test_adapter_secret_output_is_never_echoed(self):
        self.set_git()
        self.set_gh(headRefOid=COMMIT_A, statusCheckRollup=PASSING_ROLLUP)
        config = self.write_config(overrides={"tracking_projection": "trello:publyapp"})
        args = [
            "sync", "--config", config, "--pr", str(PR),
            "--projection-adapter", os.path.join(self.bin_dir, "adapter"),
        ]
        output = {
            "schema_version": 1,
            "applied": False,
            "changes": [{"type": "list_update", "summary": "secret token all-alpha-leak"}],
        }
        proc = self.run_cli(
            *args,
            extra_env=self.adapter_env(output=output, stderr="token all-alpha-leak on stderr"),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("all-alpha-leak", proc.stdout + proc.stderr)


if __name__ == "__main__":

    unittest.main()
