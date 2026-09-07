import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime

from pr_closure.model import (
    CiState,
    ClosureSnapshot,
    ClosureState,
    Evidence,
)
from pr_closure.state import derive_state
from pr_closure.sources import (
    CheckOutcome,
    CheckResult,
    CheckRunCandidate,
    GitHubIssueSource,
    GitHubSource,
    GitSource,
    LivePrSnapshot,
    PullRequestFacts,
    SourceMalformed,
    SourceUnavailable,
    WorktreeResolver,
    _parse_worktree_block,
    classify_ci,
    default_runner,
    parse_check,
    redact,
    require_infra_event,
    select_check_run_candidates,
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40
NOW = datetime(2026, 8, 8, 12, 0, 0)

PR_JSON_FIELDS = (
    "number,headRefName,headRefOid,isDraft,state,mergeStateStatus,mergeable,"
    "statusCheckRollup,url,baseRefName,body,potentialMergeCommit"
)
GH_BASE = {
    "number": 42,
    "headRefName": "feature/close",
    "baseRefName": "develop",
    "headRefOid": COMMIT_A,
    "isDraft": False,
    "state": "OPEN",
    "mergeStateStatus": "CLEAN",
    "mergeable": "MERGEABLE",
    "url": "https://github.com/owner/repo/pull/42",
    "statusCheckRollup": [],
    "body": "",
    "potentialMergeCommit": {"oid": COMMIT_B},
}
VALID_INFRA_EVENT = {
    "schema_version": 1,
    "event_type": "INFRA_FAILURE",
    "commit": COMMIT_A,
    "failed_job": "build",
    "test_steps_not_started": ["Run tests"],
    "evidence_path": "/durable/runs/events.jsonl",
}


def gh_response(**overrides):
    data = dict(GH_BASE)
    data.update(overrides)
    return (0, json.dumps(data), "")


def check_run(name, status, conclusion=None, details_url=None):
    node = {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}
    if details_url is not None:
        node["detailsUrl"] = details_url
    return node


def status_context(context, state):
    return {"__typename": "StatusContext", "context": context, "state": state}


def check_suite(app_name, status, conclusion=None):
    return {
        "__typename": "CheckSuite",
        "app": {"name": app_name, "slug": app_name.lower()},
        "status": status,
        "conclusion": conclusion,
    }


def porcelain_block(lines):
    return "\n".join(lines)


def porcelain_output(blocks):
    return "\n\n".join(blocks) + "\n"


def worktree_lines(path, head, branch=None, detached=False, extra=()):
    lines = ["worktree {0}".format(path), "HEAD {0}".format(head)]
    if branch is not None:
        lines.append("branch refs/heads/{0}".format(branch))
    elif detached:
        lines.append("detached")
    lines.extend(extra)
    return lines


def candidate_config():
    return {
        "schema_version": 1,
        "project": "publyapp",
        "repository": "owner/repo",
        "repo_path": "/var/tmp/repo",
        "default_branch": "develop",
        "closure_state_dir": "/var/tmp/state",
        "local_review_ready_commands": ["true"],
        "closure_acceptance_commands": ["true"],
        "infra_retry_budget": 1,
        "stagnation_budget_minutes": 30,
        "heavy_job_limit": 1,
        "verification_command_timeout_seconds": 30,
        "tracking_projection": None,
        "ci_required_checks": ["ci-final-gate"],
        "ci_live_pr_checks": ["ci-final-gate"],
        "ci_required_checks_source": {
            "pull_request": "candidate_tip",
            "merge_group": "event_tip",
            "push": "event_tip",
        },
        "ci_live_pr_workflow": {
            "path": ".github/workflows/ci.yml",
            "action": "pull_request",
        },
    }


class RecordingRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(tuple(argv))
        key = " ".join(argv)
        entry = self.responses.get(key)
        if entry is None and len(argv) == 3 and ("&page=" in argv[-1] or "?page=" in argv[-1]):
            endpoint = argv[-1]
            separator = "&page=" if "&page=" in endpoint else "?page="
            stem, page_text = endpoint.split(separator, 1)
            page_text = page_text.split("&", 1)[0]
            old_endpoint = stem + ("&" if "?" in stem else "?") + "per_page=100"
            legacy = "gh api --paginate --slurp " + old_endpoint
            entry = self.responses.get(legacy)
            if entry is not None and isinstance(entry[1], str):
                pages = json.loads(entry[1])
                if isinstance(pages, list):
                    page = int(page_text) - 1
                    item_key = "artifacts" if "/artifacts" in endpoint else "check_runs"
                    empty = {"total_count": pages[0].get("total_count", 0), item_key: []}
                    entry = (0, json.dumps(pages[page]) if page < len(pages) else json.dumps(empty), entry[2])
        if entry is None and "/check-suites/" in key:
            suite_id = int(key.rsplit("/", 1)[1])
            for candidate_key, candidate_entry in self.responses.items():
                if "/actions/runs/" not in candidate_key or "/attempts/" in candidate_key or "/artifacts" in candidate_key:
                    continue
                if isinstance(candidate_entry, tuple) and len(candidate_entry) == 3:
                    try:
                        workflow = json.loads(candidate_entry[1])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(workflow, dict) and workflow.get("check_suite_id") == suite_id:
                        entry = (0, json.dumps({"workflow_run": workflow}), "")
                        break
        if entry is None:
            entry = (127, "", "no scripted response for: {0}".format(key))
        if callable(entry):
            return entry(argv, timeout)
        if isinstance(entry, BaseException):
            raise entry
        return entry


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.wt = os.path.join(self.tmp, "worktree")
        self.real_wt = os.path.realpath(self.wt)
        self.branch = "feature/close"

    def git_c(self, *sub):
        return " ".join(("git", "-C", self.real_wt) + sub)

    def base_responses(self, head=COMMIT_A, branch=None, remote=COMMIT_A, status=""):
        branch = self.branch if branch is None else branch
        block = porcelain_block(worktree_lines(self.real_wt, head, branch=branch))
        responses = {
            self.git_c("worktree", "list", "--porcelain"): (0, porcelain_output([block]), ""),
            self.git_c("rev-parse", "HEAD"): (0, head + "\n", ""),
            self.git_c("rev-parse", "origin/" + branch): (0, remote + "\n", ""),
            self.git_c("status", "--porcelain=v1", "--untracked-files=all"): (0, status, ""),
        }
        return responses


class SourceFailureTests(TempDirTestCase):
    def test_gh_failure_raises_source_unavailable(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: (1, "", "HTTP 422")})
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceUnavailable):
            source.read_pr()

    def test_gh_no_pr_failure_is_never_an_empty_success(self):
        runner = RecordingRunner(
            {
                "gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: (
                    1,
                    "",
                    "GraphQL: Could not resolve to a PullRequest",
                )
            }
        )
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceUnavailable):
            source.read_pr()

    def test_gh_blank_success_is_never_a_no_pr_result(self):
        runner = RecordingRunner(
            {"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: (0, "", "")}
        )
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_git_failure_raises_source_unavailable(self):
        responses = self.base_responses()
        responses[self.git_c("worktree", "list", "--porcelain")] = (128, "", "fatal: not a git repository")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceUnavailable):
            source.discover_worktree()

    def test_timeout_raises_source_unavailable(self):
        def timed_out(argv, timeout=None):
            raise subprocess.TimeoutExpired(" ".join(argv), timeout=timeout)

        responses = self.base_responses()
        responses[self.git_c("worktree", "list", "--porcelain")] = timed_out
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses), timeout=3)
        with self.assertRaisesRegex(SourceUnavailable, "timed out"):
            source.discover_worktree()

    def test_negative_returncode_is_timeout_or_signal(self):
        responses = self.base_responses()
        responses[self.git_c("worktree", "list", "--porcelain")] = (-9, "", "Killed")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaisesRegex(SourceUnavailable, "signal"):
            source.discover_worktree()

    def test_runner_result_shape_is_validated(self):
        responses = self.base_responses()
        responses[self.git_c("worktree", "list", "--porcelain")] = (0, "worktree only")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.discover_worktree()

    def test_stdout_and_stderr_must_be_text(self):
        responses = self.base_responses()
        responses[self.git_c("worktree", "list", "--porcelain")] = (0, b"worktree bytes", None)
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.discover_worktree()


class MalformedOutputTests(TempDirTestCase):
    def test_empty_gh_output_is_malformed(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: (0, "", "")})
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_whitespace_gh_output_is_malformed(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: (0, "  \n", "")})
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_malformed_json_is_malformed(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: (0, "not json{", "")})
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_json_array_is_not_a_pr_object(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: (0, "[]", "")})
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_missing_pr_key_raises(self):
        for key in (
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
    ):
            with self.subTest(key=key):
                data = dict(GH_BASE)
                del data[key]
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: (0, json.dumps(data), "")})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaisesRegex(SourceMalformed, key):
                    source.read_pr()

    def test_pr_number_mismatch_is_malformed(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(number=7)})
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_empty_rev_parse_output_is_malformed(self):
        responses = self.base_responses()
        responses[self.git_c("rev-parse", "HEAD")] = (0, "", "")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.local_head()

    def test_whitespace_rev_parse_output_is_malformed(self):
        responses = self.base_responses()
        responses[self.git_c("rev-parse", "HEAD")] = (0, "   \n", "")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.local_head()

    def test_whitespace_only_status_is_malformed(self):
        responses = self.base_responses()
        responses[self.git_c("status", "--porcelain=v1", "--untracked-files=all")] = (0, "  \n", "")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.worktree_clean()

    def test_empty_worktree_list_is_malformed(self):
        responses = self.base_responses()
        responses[self.git_c("worktree", "list", "--porcelain")] = (0, "", "")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.discover_worktree()


class WorktreeBindingTests(TempDirTestCase):
    def test_binds_requested_resolved_path_to_exactly_one_worktree(self):
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(self.base_responses()))
        record = source.discover_worktree()
        self.assertEqual(self.real_wt, record.path)
        self.assertEqual(COMMIT_A, record.head)
        self.assertEqual(self.branch, record.branch)
        self.assertFalse(record.bare)
        self.assertFalse(record.detached)

    def test_symlink_path_resolves_to_same_worktree(self):
        real_dir = os.path.join(self.tmp, "real")
        os.makedirs(real_dir)
        link = os.path.join(self.tmp, "link")
        os.symlink(real_dir, link)
        resolved = os.path.realpath(real_dir)
        block = porcelain_block(worktree_lines(resolved, COMMIT_A, branch=self.branch))
        responses = {
            "git -C {0} worktree list --porcelain".format(resolved): (0, porcelain_output([block]), ""),
        }
        source = GitSource(link, self.branch, runner=RecordingRunner(responses))
        record = source.discover_worktree()
        self.assertEqual(resolved, record.path)

    def test_requested_path_not_a_worktree_is_malformed(self):
        block = porcelain_block(worktree_lines(os.path.join(self.tmp, "elsewhere"), COMMIT_A, branch=self.branch))
        responses = {self.git_c("worktree", "list", "--porcelain"): (0, porcelain_output([block]), "")}
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.discover_worktree()

    def test_duplicate_records_for_same_path_are_contradictory(self):
        block = porcelain_block(worktree_lines(self.real_wt, COMMIT_A, branch=self.branch))
        duplicate = porcelain_block(worktree_lines(self.real_wt, COMMIT_B, branch=self.branch))
        responses = {
            self.git_c("worktree", "list", "--porcelain"): (0, porcelain_output([block, duplicate]), "")
        }
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaisesRegex(SourceMalformed, "duplicate"):
            source.discover_worktree()

    def test_detached_and_branch_together_are_contradictory(self):
        lines = worktree_lines(self.real_wt, COMMIT_A, branch=self.branch)
        lines.append("detached")
        responses = {self.git_c("worktree", "list", "--porcelain"): (0, porcelain_output([porcelain_block(lines)]), "")}
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.discover_worktree()

    def test_unknown_worktree_field_is_malformed(self):
        lines = worktree_lines(self.real_wt, COMMIT_A, branch=self.branch) + ["bogusfield xyz"]
        responses = {self.git_c("worktree", "list", "--porcelain"): (0, porcelain_output([porcelain_block(lines)]), "")}
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.discover_worktree()

    def test_non_absolute_worktree_path_rejected(self):
        with self.assertRaises(SourceMalformed):
            GitSource("relative/worktree", self.branch)

    def test_worktree_list_argv_is_bound_to_requested_worktree(self):
        runner = RecordingRunner(self.base_responses())
        source = GitSource(self.wt, self.branch, runner=runner)
        source.discover_worktree()
        self.assertEqual(
            [("git", "-C", self.real_wt, "worktree", "list", "--porcelain")],
            runner.calls,
        )

    def test_bare_main_record_does_not_block_linked_worktree(self):
        bare_block = porcelain_block(
            ["worktree {0}".format(os.path.join(self.tmp, "main.git")), "bare"]
        )
        linked_block = porcelain_block(worktree_lines(self.real_wt, COMMIT_A, branch=self.branch))
        responses = {
            self.git_c("worktree", "list", "--porcelain"): (
                0,
                porcelain_output([bare_block, linked_block]),
                "",
            )
        }
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        record = source.discover_worktree()
        self.assertEqual(self.real_wt, record.path)
        self.assertEqual(COMMIT_A, record.head)
        self.assertEqual(self.branch, record.branch)
        self.assertFalse(record.bare)

    def test_empty_porcelain_worktree_path_rejected(self):
        block = porcelain_block(["worktree ", "HEAD " + COMMIT_A, "branch refs/heads/" + self.branch])
        responses = {
            self.git_c("worktree", "list", "--porcelain"): (0, porcelain_output([block]), "")
        }
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaisesRegex(SourceMalformed, "absolute"):
            source.discover_worktree()

    def test_relative_porcelain_worktree_path_rejected(self):
        block = porcelain_block(["worktree tools", "HEAD " + COMMIT_A, "branch refs/heads/" + self.branch])
        responses = {
            self.git_c("worktree", "list", "--porcelain"): (0, porcelain_output([block]), "")
        }
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaisesRegex(SourceMalformed, "absolute"):
            source.discover_worktree()


class BareWorktreeRecordTests(TempDirTestCase):
    def test_bare_field_line_marks_record_bare_without_head(self):
        record = _parse_worktree_block(("worktree /tmp/main.git", "bare"), ())
        self.assertTrue(record.bare)
        self.assertIsNone(record.head)
        self.assertIsNone(record.path)
        self.assertIsNone(record.branch)
        self.assertFalse(record.detached)

    def test_leading_bare_marker_marks_record_bare(self):
        record = _parse_worktree_block(("bare",), ())
        self.assertTrue(record.bare)
        self.assertIsNone(record.path)

    def test_bare_with_head_is_contradictory(self):
        for lines in (
            ("worktree /tmp/main.git", "bare", "HEAD " + COMMIT_A),
            ("worktree /tmp/main.git", "HEAD " + COMMIT_A, "bare"),
        ):
            with self.subTest(lines=lines):
                with self.assertRaisesRegex(SourceMalformed, "bare"):
                    _parse_worktree_block(lines, ())

    def test_bare_with_branch_is_contradictory(self):
        with self.assertRaisesRegex(SourceMalformed, "bare"):
            _parse_worktree_block(("worktree /tmp/main.git", "branch refs/heads/main", "bare"), ())

    def test_bare_with_detached_is_contradictory(self):
        with self.assertRaisesRegex(SourceMalformed, "bare"):
            _parse_worktree_block(("worktree /tmp/main.git", "bare", "detached"), ())

    def test_repeated_bare_field_is_contradictory(self):
        with self.assertRaisesRegex(SourceMalformed, "bare"):
            _parse_worktree_block(("worktree /tmp/main.git", "bare", "bare"), ())

    def test_bare_field_with_value_is_malformed(self):
        with self.assertRaisesRegex(SourceMalformed, "bare"):
            _parse_worktree_block(("worktree /tmp/main.git", "bare yes"), ())

    def test_non_absolute_porcelain_path_is_malformed(self):
        for block in (
            ("worktree tools", "HEAD " + COMMIT_A),
            ("worktree ", "HEAD " + COMMIT_A),
        ):
            with self.subTest(block=block):
                with self.assertRaisesRegex(SourceMalformed, "absolute"):
                    _parse_worktree_block(block, ())


class GitHeadTests(TempDirTestCase):
    def test_local_head_validated_40_lowercase_hex(self):
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(self.base_responses()))
        self.assertEqual(COMMIT_A, source.local_head())

    def test_remote_head_uses_origin_ref_and_validates(self):
        responses = self.base_responses(remote=COMMIT_B)
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        self.assertEqual(COMMIT_B, source.remote_head())

    def test_uppercase_and_wrong_length_heads_are_malformed(self):
        for bad in ("A" * 40, "a" * 39, "a" * 41, "g" * 40, "", "z" * 40):
            with self.subTest(value=bad):
                responses = self.base_responses()
                responses[self.git_c("rev-parse", "HEAD")] = (0, bad + "\n", "")
                source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
                with self.assertRaises(SourceMalformed):
                    source.local_head()

    def test_mismatched_commits_preserved_in_snapshot(self):
        responses = self.base_responses(head=COMMIT_A, remote=COMMIT_B)
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        facts = source.facts()
        pr = PullRequestFacts(
            repository="owner/repo",
            number=42,
            head_branch=self.branch,
            base_ref_name="develop",
            head_oid=COMMIT_C,
            is_draft=False,
            state="OPEN",
            merge_state_status="CLEAN",
            mergeable="MERGEABLE",
            url="https://github.com/owner/repo/pull/42",
            checks=(CheckResult("check_run", "ci", "COMPLETED", "SUCCESS", CheckOutcome.PASSING, None),),
        )
        ci = classify_ci(pr.checks, pr.head_oid)
        snapshot = ClosureSnapshot(
            evidence_available=frozenset({
                Evidence.WORKTREE,
                Evidence.LOCAL_COMMIT,
                Evidence.REMOTE_COMMIT,
                Evidence.CI,
                Evidence.CI_COMMIT,
            }),
            local_commit=facts.local_commit,
            remote_commit=facts.remote_commit,
            ci_commit=ci.ci_commit,
            ci_state=ci.ci_state,
            worktree_clean=facts.worktree_clean,
        )
        self.assertEqual(COMMIT_A, facts.local_commit)
        self.assertEqual(COMMIT_B, facts.remote_commit)
        self.assertEqual(COMMIT_C, ci.ci_commit)
        self.assertNotEqual(facts.local_commit, facts.remote_commit)
        decision = derive_state(snapshot, NOW)
        self.assertEqual(ClosureState.UNVERIFIED, decision.state)
        self.assertIn("unpushed commit", decision.reasons)
        self.assertIn("CI commit mismatch", decision.reasons)


class DirtyStateTests(TempDirTestCase):
    def test_empty_status_is_clean(self):
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(self.base_responses(status="")))
        clean, entries = source.worktree_clean()
        self.assertTrue(clean)
        self.assertEqual((), entries)

    def test_untracked_files_make_worktree_dirty(self):
        status = " M src/modified.py\n?? untracked/new.py\n?? untracked/another.md\n"
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(self.base_responses(status=status)))
        clean, entries = source.worktree_clean()
        self.assertFalse(clean)
        self.assertEqual((" M src/modified.py", "?? untracked/new.py", "?? untracked/another.md"), entries)

    def test_dirty_entries_are_preserved(self):
        status = "?? new-file.py\n"
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(self.base_responses(status=status)))
        facts = source.facts()
        self.assertFalse(facts.worktree_clean)
        self.assertIn("?? new-file.py", facts.dirty_entries)

    def test_blank_line_in_status_is_malformed(self):
        responses = self.base_responses()
        responses[self.git_c("status", "--porcelain=v1", "--untracked-files=all")] = (0, " M a.py\n\n?? b.py\n", "")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceMalformed):
            source.worktree_clean()


class GitHubPrTests(TempDirTestCase):
    def test_repository_and_number_are_bound(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response()})
        source = GitHubSource("owner/repo", 42, runner=runner)
        pr = source.read_pr()
        self.assertEqual("owner/repo", pr.repository)
        self.assertEqual(42, pr.number)
        self.assertEqual("feature/close", pr.head_branch)
        self.assertEqual("develop", pr.base_ref_name)
        self.assertEqual(COMMIT_A, pr.head_oid)
        self.assertFalse(pr.is_draft)
        self.assertEqual("OPEN", pr.state)
        self.assertEqual("CLEAN", pr.merge_state_status)
        self.assertEqual("MERGEABLE", pr.mergeable)
        self.assertEqual("https://github.com/owner/repo/pull/42", pr.url)

    def test_merge_state_status_is_required_and_non_empty(self):
        runner = RecordingRunner({
            "gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(mergeStateStatus="")
        })
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_mergeable_is_required_and_non_empty(self):
        runner = RecordingRunner({
            "gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(mergeable="")
        })
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_unknown_mergeable_state_is_source_malformed(self):
        runner = RecordingRunner({
            "gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(mergeable="MAYBE")
        })
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_base_ref_name_is_bound_and_validated(self):
        for bad in ("", "   ", 42, None, ["develop"]):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(baseRefName=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_invalid_repository_shape_rejected(self):
        for bad in ("norepo", "/repo", "owner/", "owner repo/x", ""):
            with self.subTest(value=bad):
                with self.assertRaises(SourceMalformed):
                    GitHubSource(bad, 42)

    def test_invalid_pr_number_rejected(self):
        for bad in (0, -1, 1.5, "42", None, True):
            with self.subTest(value=bad):
                with self.assertRaises(SourceMalformed):
                    GitHubSource("owner/repo", bad)

    def test_head_oid_validated(self):
        for bad in ("A" * 40, "a" * 39, "g" * 40, ""):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(headRefOid=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_is_draft_must_be_boolean(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(isDraft="yes")})
        source = GitHubSource("owner/repo", 42, runner=runner)
        with self.assertRaises(SourceMalformed):
            source.read_pr()

    def test_unknown_pr_state_rejected(self):
        for bad in ("OPENED", "DRAFT", "MERGING", "closed", "PENDING"):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(state=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_url_must_bind_repository_and_number(self):
        for bad in (
            "https://github.com/other/repo/pull/42",
            "https://github.com/owner/repo/pull/43",
            "https://github.com/owner/repo/issues/42",
            "not-a-url",
            "",
        ):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(url=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_url_rejects_non_github_host(self):
        for bad in (
            "https://github.example/owner/repo/pull/42",
            "https://evil.com/owner/repo/pull/42",
            "https://github.com.evil.com/owner/repo/pull/42",
            "https://github%2ecom/owner/repo/pull/42",
        ):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(url=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_url_rejects_insecure_scheme_and_credentials(self):
        for bad in (
            "http://github.com/owner/repo/pull/42",
            "ftp://github.com/owner/repo/pull/42",
            "https://user@github.com/owner/repo/pull/42",
            "https://user:pass@github.com/owner/repo/pull/42",
        ):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(url=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_url_rejects_non_default_port(self):
        for bad in (
            "https://github.com:8080/owner/repo/pull/42",
            "https://github.com:80/owner/repo/pull/42",
        ):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(url=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_url_rejects_lookalike_and_encoded_paths(self):
        for bad in (
            "https://github.com//owner/repo/pull/42",
            "https://github.com/ownerx/repo/pull/42",
            "https://github.com/owner/repo/pull/420",
            "https://github.com/owner/repo/pulls/42",
            "https://github.com/owner/repo/pull/42/extra",
            "https://github.com/owner/repo/pull/42/x",
            "https://github.com/owner/repo/pull/42/.",
            "https://github.com/owner/repo/pull/42/..",
            "https://github.com/owner/repo/pull/%34%32",
            "https://github.com/%6Fwner/repo/pull/42",
        ):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(url=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_url_rejects_query_and_fragment(self):
        for bad in (
            "https://github.com/owner/repo/pull/42?x=1",
            "https://github.com/owner/repo/pull/42#frag",
        ):
            with self.subTest(value=bad):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(url=bad)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                with self.assertRaises(SourceMalformed):
                    source.read_pr()

    def test_url_accepts_canonical_github_origin(self):
        for ok in (
            "https://github.com/owner/repo/pull/42",
            "https://github.com/owner/repo/pull/42/",
            "https://GitHub.com/owner/repo/pull/42",
            "https://GITHUB.COM/owner/repo/pull/42",
            "https://github.com:443/owner/repo/pull/42",
        ):
            with self.subTest(value=ok):
                runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response(url=ok)})
                source = GitHubSource("owner/repo", 42, runner=runner)
                pr = source.read_pr()
                self.assertEqual(ok, pr.url)

    def test_argv_is_exact_gh_invocation_without_shell(self):
        runner = RecordingRunner({"gh pr view 42 --repo owner/repo --json " + PR_JSON_FIELDS: gh_response()})
        source = GitHubSource("owner/repo", 42, runner=runner)
        source.read_pr()
        self.assertEqual(
            [("gh", "pr", "view", "42", "--repo", "owner/repo", "--json", PR_JSON_FIELDS)],
            runner.calls,
        )


class GitHubCandidateTipTests(TempDirTestCase):
    def test_candidate_config_is_read_at_exact_head_and_blob_bound_to_tree(self):
        raw = json.dumps(candidate_config()).encode()
        encoded = base64.b64encode(raw).decode()
        content_key = (
            "gh api repos/owner/repo/contents/"
            ".ai/project-closure-v1.json?ref=" + COMMIT_A
        )
        tree_key = (
            "gh api repos/owner/repo/git/trees/"
            + COMMIT_A
            + "?recursive=1"
        )
        runner = RecordingRunner(
            {
                content_key: (
                    0,
                    json.dumps({
                        "path": ".ai/project-closure-v1.json",
                        "sha": "d" * 40,
                        "encoding": "base64",
                        "content": encoded,
                    }),
                    "",
                ),
                tree_key: (
                    0,
                    json.dumps({
                        "truncated": False,
                        "tree": [{
                            "path": ".ai/project-closure-v1.json",
                            "type": "blob",
                            "sha": "d" * 40,
                        }],
                    }),
                    "",
                ),
            }
        )
        source = GitHubSource("owner/repo", 42, runner=runner)
        self.assertEqual(candidate_config(), source.read_candidate_tip_config(COMMIT_A))
        self.assertEqual(
            [
                tuple(content_key.split()),
                tuple(tree_key.split()),
            ],
            runner.calls,
        )

    def test_candidate_config_blob_tree_mismatch_fails_closed(self):
        raw = base64.b64encode(json.dumps(candidate_config()).encode()).decode()
        content_key = (
            "gh api repos/owner/repo/contents/"
            ".ai/project-closure-v1.json?ref=" + COMMIT_A
        )
        tree_key = (
            "gh api repos/owner/repo/git/trees/"
            + COMMIT_A
            + "?recursive=1"
        )
        runner = RecordingRunner({
            content_key: (
                0,
                json.dumps({
                    "path": ".ai/project-closure-v1.json",
                    "sha": "d" * 40,
                    "encoding": "base64",
                    "content": raw,
                }),
                "",
            ),
            tree_key: (
                0,
                json.dumps({
                    "truncated": False,
                    "tree": [{
                        "path": ".ai/project-closure-v1.json",
                        "type": "blob",
                        "sha": "e" * 40,
                    }],
                }),
                "",
            ),
        })
        with self.assertRaisesRegex(SourceMalformed, "blob identity"):
            GitHubSource("owner/repo", 42, runner=runner).read_candidate_tip_config(COMMIT_A)

    def test_check_run_reads_all_pages_and_validates_workflow_provenance(self):
        workflow_key = (
            "gh api repos/owner/repo/actions/workflows?per_page=100"
        )
        check_key = (
            "gh api --paginate --slurp repos/owner/repo/commits/"
            + COMMIT_A
            + "/check-runs?filter=all&per_page=100"
        )
        run_key = "gh api repos/owner/repo/actions/runs/201"
        attempt_key = "gh api repos/owner/repo/actions/runs/201/attempts/2"
        run = {
            "id": 201,
            "workflow_id": 77,
            "path": ".github/workflows/ci.yml@main",
            "event": "pull_request",
            "run_attempt": 2,
            "head_sha": COMMIT_A,
            "check_suite_id": 301,
        }
        raw = {
            "id": 101,
            "name": "ci-final-gate",
            "head_sha": COMMIT_A,
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-09-05T10:00:00Z",
            "completed_at": "2026-09-05T10:01:00Z",
            "details_url": "https://github.com/owner/repo/actions/runs/201/job/9",
            "app": {"slug": "github-actions"},
            "check_suite": {"id": 301},
        }
        runner = RecordingRunner({
            workflow_key: (0, json.dumps({"total_count": 1, "workflows": [{"id": 77, "path": ".github/workflows/ci.yml"}]}), ""),
            check_key: (
                0,
                json.dumps([
                    {"total_count": 1, "check_runs": [raw]},
                ]),
                "",
            ),
            run_key: (0, json.dumps(run), ""),
            attempt_key: (0, json.dumps(dict(run, check_suite_id=301)), ""),
        })
        candidates = GitHubSource("owner/repo", 42, runner=runner).read_check_run_candidates(
            COMMIT_A,
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual(101, candidates[0].result.check_run_id)
        self.assertEqual(201, candidates[0].result.workflow_run_id)
        self.assertEqual(2, candidates[0].result.run_attempt)
        self.assertIn("filter=all", check_key)


class GitHubLiveCiTests(TempDirTestCase):
    def pr(self, body="body", draft=False):
        return PullRequestFacts(
            repository="owner/repo",
            number=42,
            head_branch="feature/close",
            base_ref_name="develop",
            head_oid=COMMIT_A,
            is_draft=draft,
            state="OPEN",
            merge_state_status="CLEAN",
            mergeable="MERGEABLE",
            url="https://github.com/owner/repo/pull/42",
            checks=(),
            body=body,
            potential_merge_commit_oid=COMMIT_B,
        )

    def runner(self, artifacts, extra_raw=()):
        workflow_key = "gh api repos/owner/repo/actions/workflows?per_page=100"
        check_key = (
            "gh api --paginate --slurp repos/owner/repo/commits/"
            + COMMIT_A
            + "/check-runs?filter=all&per_page=100"
        )
        run_key = "gh api repos/owner/repo/actions/runs/201"
        attempt_key = "gh api repos/owner/repo/actions/runs/201/attempts/1"
        artifacts_key = (
            "gh api --paginate --slurp "
            "repos/owner/repo/actions/runs/201/artifacts?per_page=100"
        )
        raw = {
            "id": 101,
            "name": "ci-final-gate",
            "head_sha": COMMIT_A,
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-09-05T10:00:00Z",
            "completed_at": "2026-09-05T10:01:00Z",
            "details_url": "https://github.com/owner/repo/actions/runs/201/job/9",
            "app": {"slug": "github-actions"},
            "check_suite": {"id": 301},
        }
        responses = {
            workflow_key: (0, json.dumps({"total_count": 1, "workflows": [{"id": 77, "path": ".github/workflows/ci.yml"}]}), ""),
            check_key: (
                0,
                json.dumps([{"total_count": len(extra_raw) + 1, "check_runs": [*extra_raw, raw]}]),
                "",
            ),
            run_key: (
                0,
                json.dumps({
                    "id": 201,
                    "workflow_id": 77,
                    "path": ".github/workflows/ci.yml@main",
                    "event": "pull_request",
                    "run_attempt": 1,
                    "head_sha": COMMIT_A,
                    "check_suite_id": 301,
                }),
                "",
            ),
            attempt_key: (
                0,
                json.dumps({
                    "id": 201,
                    "workflow_id": 77,
                    "path": ".github/workflows/ci.yml@main",
                    "event": "pull_request",
                    "run_attempt": 1,
                    "head_sha": COMMIT_A,
                    "check_suite_id": 301,
                }),
                "",
            ),
            artifacts_key: (
                0,
                json.dumps([{"total_count": len(artifacts), "artifacts": artifacts}]),
                "",
            ),
        }
        return RecordingRunner(responses)

    def snapshot(self, body="body", event_sha=COMMIT_B):
        return {
            "pr_number": 42,
            "head_sha": COMMIT_A,
            "base_ref_name": "develop",
            "potential_merge_commit_oid": COMMIT_B,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "is_draft": False,
            "event_name": "pull_request",
            "event_sha": event_sha,
            "workflow_path": ".github/workflows/ci.yml",
            "workflow_id": 77,
            "workflow_action": "pull_request",
            "run_id": 201,
            "run_attempt": 1,
        }

    def test_realistic_head_and_merge_event_pair_passes(self):
        reader = lambda _run, _name: self.snapshot()
        source = GitHubSource(
            "owner/repo",
            42,
            runner=self.runner([{"id": 501, "name": "ci-pr-snapshot-201-1", "expired": False, "size_in_bytes": 256}]),
            artifact_reader=reader,
        )
        facts = source.read_live_ci(
            self.pr(),
            required_checks=("ci-final-gate",),
            live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
        )
        self.assertEqual(CiState.PASSING, facts.ci_state)
        self.assertEqual(101, facts.check_run_id)
        self.assertEqual(201, facts.workflow_run_id)
        self.assertEqual(COMMIT_A, facts.head_sha)
        self.assertEqual(COMMIT_B, facts.event_sha)

    def test_live_provenance_uses_the_live_context_not_first_required_check(self):
        older_required = {
            "id": 100,
            "name": "legacy-gate",
            "head_sha": COMMIT_A,
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-09-05T09:00:00Z",
            "completed_at": "2026-09-05T09:01:00Z",
            "details_url": "https://github.com/owner/repo/actions/runs/201/job/8",
            "app": {"slug": "github-actions"},
            "check_suite": {"id": 301},
        }
        source = GitHubSource(
            "owner/repo",
            42,
            runner=self.runner(
                [{"id": 501, "name": "ci-pr-snapshot-201-1", "expired": False, "size_in_bytes": 256}],
                extra_raw=(older_required,),
            ),
            artifact_reader=lambda _run, _name: self.snapshot(),
        )
        facts = source.read_live_ci(
            self.pr(),
            required_checks=("legacy-gate", "ci-final-gate"),
            live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
        )
        self.assertEqual(CiState.PASSING, facts.ci_state)
        self.assertEqual(101, facts.check_run_id)
        self.assertEqual(201, facts.workflow_run_id)

    def test_missing_or_duplicate_snapshot_is_unverified(self):
        for artifacts in ([], [
            {"id": 501, "name": "ci-pr-snapshot-201-1", "expired": False, "size_in_bytes": 256},
            {"id": 502, "name": "ci-pr-snapshot-201-1", "expired": False, "size_in_bytes": 256},
        ]):
            with self.subTest(artifacts=artifacts):
                source = GitHubSource(
                    "owner/repo",
                    42,
                    runner=self.runner(artifacts),
                    artifact_reader=lambda _run, _name: self.snapshot(),
                )
                facts = source.read_live_ci(
                    self.pr(),
                    required_checks=("ci-final-gate",),
                    live_checks=("ci-final-gate",),
                    workflow_path=".github/workflows/ci.yml",
                    workflow_action="pull_request",
                )
                self.assertEqual(CiState.UNKNOWN, facts.ci_state)
                self.assertTrue(any("snapshot" in reason for reason in facts.reasons))


class WorktreeResolverTests(TempDirTestCase):
    def resolver(self, anchor, blocks, **kwargs):
        responses = {
            "git -C {0} worktree list --porcelain".format(anchor): (
                0,
                porcelain_output([porcelain_block(block) for block in blocks]),
                "",
            )
        }
        return WorktreeResolver(anchor, runner=RecordingRunner(responses), **kwargs)

    def test_resolves_exactly_one_worktree_by_checked_out_branch(self):
        anchor = os.path.join(self.tmp, "main")
        pr_wt = os.path.join(self.tmp, "elsewhere", "pr")
        resolver = self.resolver(
            anchor,
            [
                ["worktree " + anchor, "HEAD " + COMMIT_A, "branch refs/heads/develop"],
                ["worktree " + pr_wt, "HEAD " + COMMIT_A, "branch refs/heads/feature/close"],
            ],
        )
        record = resolver.resolve("feature/close")
        self.assertEqual(pr_wt, record.path)
        self.assertEqual("feature/close", record.branch)
        self.assertEqual(COMMIT_A, record.head)
        self.assertFalse(record.bare)

    def test_resolved_path_is_never_constructed(self):
        anchor = os.path.join(self.tmp, "main")
        pr_wt = os.path.join(self.tmp, "elsewhere", "pr")
        resolver = self.resolver(
            anchor,
            [
                ["worktree " + anchor, "HEAD " + COMMIT_A, "branch refs/heads/develop"],
                ["worktree " + pr_wt, "HEAD " + COMMIT_A, "branch refs/heads/feature/close"],
            ],
        )
        record = resolver.resolve("feature/close")
        self.assertEqual(pr_wt, record.path)
        self.assertNotEqual(anchor, record.path)

    def test_missing_branch_binding_fails_closed(self):
        anchor = os.path.join(self.tmp, "main")
        resolver = self.resolver(
            anchor,
            [["worktree " + anchor, "HEAD " + COMMIT_A, "branch refs/heads/develop"]],
        )
        with self.assertRaisesRegex(SourceMalformed, "feature/close"):
            resolver.resolve("feature/close")

    def test_duplicate_branch_binding_fails_closed(self):
        anchor = os.path.join(self.tmp, "main")
        resolver = self.resolver(
            anchor,
            [
                ["worktree " + anchor, "HEAD " + COMMIT_A, "branch refs/heads/develop"],
                ["worktree " + os.path.join(self.tmp, "one"), "HEAD " + COMMIT_A, "branch refs/heads/feature/close"],
                ["worktree " + os.path.join(self.tmp, "two"), "HEAD " + COMMIT_A, "branch refs/heads/feature/close"],
            ],
        )
        with self.assertRaisesRegex(SourceMalformed, "duplicate"):
            resolver.resolve("feature/close")

    def test_detached_worktree_never_matches(self):
        anchor = os.path.join(self.tmp, "main")
        resolver = self.resolver(
            anchor,
            [
                ["worktree " + anchor, "HEAD " + COMMIT_A, "branch refs/heads/develop"],
                ["worktree " + os.path.join(self.tmp, "wt"), "HEAD " + COMMIT_A, "detached"],
            ],
        )
        with self.assertRaises(SourceMalformed):
            resolver.resolve("feature/close")

    def test_bare_worktree_never_matches(self):
        anchor = os.path.join(self.tmp, "main")
        resolver = self.resolver(
            anchor,
            [
                ["worktree " + anchor, "HEAD " + COMMIT_A, "branch refs/heads/develop"],
                ["worktree " + os.path.join(self.tmp, "bare.git"), "bare"],
            ],
        )
        with self.assertRaises(SourceMalformed):
            resolver.resolve("feature/close")

    def test_resolution_uses_the_anchor_for_worktree_list(self):
        anchor = os.path.join(self.tmp, "main")
        runner = RecordingRunner(
            {
                "git -C {0} worktree list --porcelain".format(anchor): (
                    0,
                    porcelain_output(
                        [
                            porcelain_block(
                                ["worktree " + anchor, "HEAD " + COMMIT_A, "branch refs/heads/develop"]
                            )
                        ]
                    ),
                    "",
                )
            }
        )
        resolver = WorktreeResolver(anchor, runner=runner)
        with self.assertRaises(SourceMalformed):
            resolver.resolve("feature/close")
        self.assertEqual(
            [("git", "-C", anchor, "worktree", "list", "--porcelain")],
            runner.calls,
        )

    def test_anchor_must_be_absolute(self):
        with self.assertRaises(SourceMalformed):
            WorktreeResolver("relative/anchor")

    def test_empty_worktree_output_fails_closed(self):
        anchor = os.path.join(self.tmp, "main")
        runner = RecordingRunner(
            {"git -C {0} worktree list --porcelain".format(anchor): (0, "", "")}
        )
        resolver = WorktreeResolver(anchor, runner=runner)
        with self.assertRaises(SourceMalformed):
            resolver.resolve("feature/close")


class GitHubIssueSourceTests(TempDirTestCase):
    ISSUE_JSON_FIELDS = "number,state,url"

    def runner_for(self, payload):
        return RecordingRunner(
            {"gh issue view 101 --repo owner/repo --json " + self.ISSUE_JSON_FIELDS: (0, payload, "")}
        )

    def test_read_issue_binds_number_repository_state_and_url(self):
        payload = json.dumps(
            {"number": 101, "state": "OPEN", "url": "https://github.com/owner/repo/issues/101"}
        )
        issue = GitHubIssueSource("owner/repo", 101, runner=self.runner_for(payload)).read_issue()
        self.assertEqual("owner/repo", issue.repository)
        self.assertEqual(101, issue.number)
        self.assertEqual("OPEN", issue.state)

    def test_issue_number_mismatch_is_malformed(self):
        payload = json.dumps(
            {"number": 102, "state": "OPEN", "url": "https://github.com/owner/repo/issues/102"}
        )
        source = GitHubIssueSource("owner/repo", 101, runner=self.runner_for(payload))
        with self.assertRaises(SourceMalformed):
            source.read_issue()

    def test_unknown_issue_state_is_malformed(self):
        for bad in ("DONE", "PENDING", "merged", ""):
            with self.subTest(state=bad):
                payload = json.dumps(
                    {"number": 101, "state": bad, "url": "https://github.com/owner/repo/issues/101"}
                )
                source = GitHubIssueSource("owner/repo", 101, runner=self.runner_for(payload))
                with self.assertRaises(SourceMalformed):
                    source.read_issue()

    def test_issue_url_must_bind_repository_and_number(self):
        for bad in (
            "https://github.com/other/repo/issues/101",
            "https://github.com/owner/repo/issues/102",
            "https://github.com/owner/repo/pull/101",
            "https://evil.com/owner/repo/issues/101",
        ):
            with self.subTest(url=bad):
                payload = json.dumps({"number": 101, "state": "OPEN", "url": bad})
                source = GitHubIssueSource("owner/repo", 101, runner=self.runner_for(payload))
                with self.assertRaises(SourceMalformed):
                    source.read_issue()

    def test_issue_missing_keys_are_malformed(self):
        for key in ("number", "state", "url"):
            with self.subTest(key=key):
                data = {"number": 101, "state": "OPEN", "url": "https://github.com/owner/repo/issues/101"}
                del data[key]
                source = GitHubIssueSource("owner/repo", 101, runner=self.runner_for(json.dumps(data)))
                with self.assertRaisesRegex(SourceMalformed, key):
                    source.read_issue()

    def test_issue_source_argv_is_exact_without_shell(self):
        payload = json.dumps(
            {"number": 101, "state": "OPEN", "url": "https://github.com/owner/repo/issues/101"}
        )
        runner = self.runner_for(payload)
        GitHubIssueSource("owner/repo", 101, runner=runner).read_issue()
        self.assertEqual(
            [("gh", "issue", "view", "101", "--repo", "owner/repo", "--json", self.ISSUE_JSON_FIELDS)],
            runner.calls,
        )

    def test_invalid_repository_or_number_rejected(self):
        with self.assertRaises(SourceMalformed):
            GitHubIssueSource("norepo", 101)
        with self.assertRaises(SourceMalformed):
            GitHubIssueSource("owner/repo", 0)


class CheckRollupTests(TempDirTestCase):
    def test_check_run_passing(self):
        pr = PullRequestFacts(
            repository="owner/repo", number=42, head_branch="f", base_ref_name="develop",
            head_oid=COMMIT_A, is_draft=False, state="OPEN", url="u",
            merge_state_status="CLEAN",
            mergeable="MERGEABLE",
            checks=(parse_check(check_run("lint", "COMPLETED", "SUCCESS")),),
        )
        self.assertEqual(CheckOutcome.PASSING, pr.checks[0].outcome)

    def test_check_run_failure(self):
        result = parse_check(check_run("lint", "COMPLETED", "FAILURE"))
        self.assertEqual(CheckOutcome.FAILURE, result.outcome)

    def test_check_run_pending(self):
        for status in ("QUEUED", "IN_PROGRESS", "PENDING"):
            with self.subTest(status=status):
                result = parse_check(check_run("lint", status))
                self.assertEqual(CheckOutcome.PENDING, result.outcome)

    def test_check_run_pending_accepts_github_empty_conclusion(self):
        result = parse_check(check_run("lint", "IN_PROGRESS", ""))
        self.assertEqual(CheckOutcome.PENDING, result.outcome)
        self.assertIsNone(result.conclusion)

    def test_check_run_skipped(self):
        result = parse_check(check_run("lint", "COMPLETED", "SKIPPED"))
        self.assertEqual(CheckOutcome.SKIPPED, result.outcome)

    def test_status_context_success_and_failure(self):
        ok = parse_check(status_context("ci/circleci", "SUCCESS"))
        bad = parse_check(status_context("ci/circleci", "FAILURE"))
        self.assertEqual(CheckOutcome.PASSING, ok.outcome)
        self.assertEqual(CheckOutcome.FAILURE, bad.outcome)
        self.assertEqual("ci/circleci", ok.name)
        self.assertEqual("status_context", ok.node_type)

    def test_status_context_target_url_used_as_fallback(self):
        node = {
            "__typename": "StatusContext",
            "context": "ci/circleci",
            "state": "FAILURE",
            "targetUrl": "https://example.com/logs",
        }
        result = parse_check(node)
        self.assertEqual("https://example.com/logs", result.details_url)

    def test_status_context_target_url_must_be_string(self):
        for bad in (42, True, 1.5, [], {}):
            with self.subTest(value=bad):
                node = {
                    "__typename": "StatusContext",
                    "context": "ci/circleci",
                    "state": "FAILURE",
                    "targetUrl": bad,
                }
                with self.assertRaises(SourceMalformed):
                    parse_check(node)

    def test_check_suite_uses_app_name(self):
        result = parse_check(check_suite("build", "COMPLETED", "SUCCESS"))
        self.assertEqual("build", result.name)
        self.assertEqual(CheckOutcome.PASSING, result.outcome)
        self.assertEqual("check_suite", result.node_type)

    def test_unknown_check_conclusion_is_malformed(self):
        with self.assertRaises(SourceMalformed):
            parse_check(check_run("lint", "COMPLETED", "DONE_SOON"))

    def test_unknown_check_status_is_malformed(self):
        with self.assertRaises(SourceMalformed):
            parse_check(check_run("lint", "FINISHED"))

    def test_unknown_node_type_is_malformed(self):
        with self.assertRaises(SourceMalformed):
            parse_check({"__typename": "Magic", "name": "x", "status": "COMPLETED"})

    def test_completed_run_without_conclusion_is_malformed(self):
        with self.assertRaises(SourceMalformed):
            parse_check(check_run("lint", "COMPLETED"))

    def test_node_must_be_an_object(self):
        with self.assertRaises(SourceMalformed):
            parse_check(["not", "an", "object"])

    def test_missing_node_type_is_malformed(self):
        with self.assertRaises(SourceMalformed):
            parse_check({"name": "x", "status": "COMPLETED", "conclusion": "SUCCESS"})


class CiClassificationTests(TempDirTestCase):
    def all_green_checks(self):
        return (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("test", "COMPLETED", "SUCCESS")),
        )

    def test_single_failure_not_hidden_by_green_aggregate(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("test", "COMPLETED", "FAILURE")),
        )
        ci = classify_ci(checks, COMMIT_A)
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertEqual(COMMIT_A, ci.ci_commit)
        self.assertIsNone(ci.infra_job)

    def test_all_green_is_passing(self):
        ci = classify_ci(self.all_green_checks(), COMMIT_A)
        self.assertEqual(CiState.PASSING, ci.ci_state)

    def test_pending_checks_stay_pending(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("test", "IN_PROGRESS")),
        )
        ci = classify_ci(checks, COMMIT_A)
        self.assertEqual(CiState.PENDING, ci.ci_state)

    def test_skipped_checks_do_not_become_passing_evidence(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("test", "COMPLETED", "SKIPPED")),
        )
        ci = classify_ci(checks, COMMIT_A)
        self.assertNotEqual(CiState.PASSING, ci.ci_state)
        self.assertEqual(CiState.UNKNOWN, ci.ci_state)

    def test_empty_rollup_is_unknown(self):
        ci = classify_ci((), COMMIT_A)
        self.assertEqual(CiState.UNKNOWN, ci.ci_state)

    # -- required-check policy: opt-in exact selection --

    def test_required_checks_green_set_with_unrelated_skipped_is_passing(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("docs", "COMPLETED", "SKIPPED")),
        )
        ci = classify_ci(checks, COMMIT_A, required_checks=("lint",))
        self.assertEqual(CiState.PASSING, ci.ci_state)
        self.assertEqual(2, len(ci.checks))

    def test_required_checks_missing_name_is_never_passing(self):
        checks = (parse_check(check_run("lint", "COMPLETED", "SUCCESS")),)
        ci = classify_ci(checks, COMMIT_A, required_checks=("lint", "missing-gate"))
        self.assertEqual(CiState.UNKNOWN, ci.ci_state)
        self.assertTrue(any("missing" in reason for reason in ci.reasons))

    def test_required_checks_duplicate_live_result_is_never_passing(self):
        checks = (
            parse_check(check_run("build", "COMPLETED", "SUCCESS")),
            parse_check(check_run("build", "COMPLETED", "SUCCESS")),
        )
        ci = classify_ci(checks, COMMIT_A, required_checks=("build",))
        self.assertEqual(CiState.UNKNOWN, ci.ci_state)
        self.assertTrue(any("duplicate" in reason for reason in ci.reasons))

    def test_required_checks_selected_failure_is_branch_failure(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("test", "COMPLETED", "FAILURE")),
            parse_check(check_run("docs", "COMPLETED", "SKIPPED")),
        )
        ci = classify_ci(checks, COMMIT_A, required_checks=("lint", "test"))
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertIsNone(ci.infra_job)

    def test_required_checks_selected_skipped_cannot_pass(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SKIPPED")),
            parse_check(check_run("test", "COMPLETED", "SUCCESS")),
        )
        ci = classify_ci(checks, COMMIT_A, required_checks=("lint", "test"))
        self.assertEqual(CiState.UNKNOWN, ci.ci_state)

    def test_required_checks_selected_pending_is_pending(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("test", "IN_PROGRESS")),
        )
        ci = classify_ci(checks, COMMIT_A, required_checks=("lint", "test"))
        self.assertEqual(CiState.PENDING, ci.ci_state)

    def test_required_checks_unrelated_failure_is_non_authoritative(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("cleanup", "COMPLETED", "FAILURE")),
        )
        ci = classify_ci(checks, COMMIT_A, required_checks=("lint",))
        self.assertEqual(CiState.PASSING, ci.ci_state)

    def test_empty_required_checks_keeps_all_rollup_behavior(self):
        checks = (
            parse_check(check_run("lint", "COMPLETED", "SUCCESS")),
            parse_check(check_run("test", "COMPLETED", "SKIPPED")),
        )
        ci = classify_ci(checks, COMMIT_A, required_checks=())
        self.assertEqual(CiState.UNKNOWN, ci.ci_state)

    def test_required_checks_infra_uses_only_selected_failure(self):
        checks = (
            parse_check(check_run("build", "COMPLETED", "FAILURE")),
            parse_check(check_run("cleanup", "COMPLETED", "FAILURE")),
        )
        ci = classify_ci(
            checks, COMMIT_A, infra_event=VALID_INFRA_EVENT, required_checks=("build",)
        )
        self.assertEqual(CiState.INFRA_FAILURE, ci.ci_state)
        self.assertEqual("build", ci.infra_job)

    def test_infra_event_authorizes_infrastructure_classification(self):
        checks = (parse_check(check_run("build", "COMPLETED", "FAILURE")),)
        ci = classify_ci(checks, COMMIT_A, infra_event=VALID_INFRA_EVENT)
        self.assertEqual(CiState.INFRA_FAILURE, ci.ci_state)
        self.assertEqual("build", ci.infra_job)

    def test_infra_event_for_wrong_job_stays_branch_failure(self):
        checks = (parse_check(check_run("test", "COMPLETED", "FAILURE")),)
        event = dict(VALID_INFRA_EVENT, failed_job="build")
        ci = classify_ci(checks, COMMIT_A, infra_event=event)
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertIsNone(ci.infra_job)

    def test_infra_event_without_test_proof_stays_branch_failure(self):
        checks = (parse_check(check_run("build", "COMPLETED", "FAILURE")),)
        event = dict(VALID_INFRA_EVENT, test_steps_not_started=[])
        ci = classify_ci(checks, COMMIT_A, infra_event=event)
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertIsNone(ci.infra_job)

    def test_malformed_infra_event_raises(self):
        checks = (parse_check(check_run("build", "COMPLETED", "FAILURE")),)
        for event in (
            [],
            dict(VALID_INFRA_EVENT, event_type="COMMIT"),
            dict(VALID_INFRA_EVENT, failed_job=""),
            dict(VALID_INFRA_EVENT, test_steps_not_started="Run tests"),
            dict(VALID_INFRA_EVENT, test_steps_not_started=[None]),
            dict(VALID_INFRA_EVENT, test_steps_not_started=[1]),
            dict(VALID_INFRA_EVENT, commit="abc"),
            dict(VALID_INFRA_EVENT, evidence_path=""),
        ):
            with self.subTest(event=event):
                with self.assertRaises(SourceMalformed):
                    classify_ci(checks, COMMIT_A, infra_event=event)

    def test_absent_infra_event_stays_branch_failure(self):
        checks = (parse_check(check_run("build", "COMPLETED", "FAILURE")),)
        ci = classify_ci(checks, COMMIT_A, infra_event=None)
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertIsNone(ci.infra_job)

    def test_infra_event_ignored_when_rollup_is_green(self):
        ci = classify_ci(self.all_green_checks(), COMMIT_A, infra_event=VALID_INFRA_EVENT)
        self.assertEqual(CiState.PASSING, ci.ci_state)
        self.assertIsNone(ci.infra_job)

    def test_infra_event_for_stale_commit_stays_branch_failure(self):
        checks = (parse_check(check_run("build", "COMPLETED", "FAILURE")),)
        event = dict(VALID_INFRA_EVENT, commit=COMMIT_B)
        ci = classify_ci(checks, COMMIT_A, infra_event=event)
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertIsNone(ci.infra_job)
        self.assertTrue(any("commit" in reason for reason in ci.reasons))

    def test_infra_event_for_cross_pr_commit_stays_branch_failure(self):
        checks = (parse_check(check_run("build", "COMPLETED", "FAILURE")),)
        event = dict(VALID_INFRA_EVENT, commit=COMMIT_C)
        ci = classify_ci(checks, COMMIT_A, infra_event=event)
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertIsNone(ci.infra_job)

    def test_infra_event_does_not_hide_unexplained_second_failure(self):
        checks = (
            parse_check(check_run("build", "COMPLETED", "FAILURE")),
            parse_check(check_run("test", "COMPLETED", "FAILURE")),
        )
        ci = classify_ci(checks, COMMIT_A, infra_event=VALID_INFRA_EVENT)
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertIsNone(ci.infra_job)
        self.assertTrue(any("checks failed" in reason for reason in ci.reasons))

    def test_infra_event_does_not_hide_duplicate_same_name_failures(self):
        checks = (
            parse_check(check_run("build", "COMPLETED", "FAILURE")),
            parse_check(check_run("build", "COMPLETED", "FAILURE")),
        )
        ci = classify_ci(checks, COMMIT_A, infra_event=VALID_INFRA_EVENT)
        self.assertEqual(CiState.BRANCH_FAILURE, ci.ci_state)
        self.assertIsNone(ci.infra_job)


class LiveCheckRunSelectionTests(TempDirTestCase):
    def candidate(
        self,
        check_id,
        suite_id,
        run_id,
        name="ci-final-gate",
        status="COMPLETED",
        conclusion="SUCCESS",
        started_at="2026-09-05T10:00:00Z",
        completed_at="2026-09-05T10:01:00Z",
    ):
        outcome = CheckOutcome.PASSING if conclusion == "SUCCESS" else CheckOutcome.FAILURE
        return CheckRunCandidate(
            result=CheckResult(
                "check_run",
                name,
                status,
                conclusion,
                outcome,
                "https://github.com/owner/repo/actions/runs/{0}/job/9".format(run_id),
                check_run_id=check_id,
                head_sha=COMMIT_A,
                started_at=started_at,
                completed_at=completed_at,
                app_slug="github-actions",
                check_suite_id=suite_id,
                workflow_run_id=run_id,
                workflow_id=77,
                workflow_path=".github/workflows/ci.yml",
                workflow_action="pull_request",
                workflow_event="pull_request",
                run_attempt=1,
            )
        )

    def test_selects_latest_by_start_completion_then_check_run_id(self):
        selected = select_check_run_candidates(
            [
                self.candidate(101, 1, 201, completed_at="2026-09-05T10:02:00Z"),
                self.candidate(
                    103,
                    1,
                    203,
                    started_at="2026-09-05T10:01:00Z",
                    completed_at="2026-09-05T10:01:30Z",
                ),
                self.candidate(
                    102,
                    1,
                    202,
                    started_at="2026-09-05T10:01:00Z",
                    completed_at="2026-09-05T10:01:30Z",
                ),
            ],
            ("ci-final-gate",),
            head_oid=COMMIT_A,
        )
        self.assertEqual(103, selected.checks[0].check_run_id)
        self.assertEqual(203, selected.checks[0].workflow_run_id)

    def test_tied_latest_start_across_suites_is_unverified(self):
        facts = select_check_run_candidates(
            [self.candidate(101, 1, 201), self.candidate(102, 2, 202)],
            ("ci-final-gate",),
            head_oid=COMMIT_A,
        )
        self.assertEqual(CiState.UNKNOWN, facts.ci_state)
        self.assertTrue(any("ambiguous concurrent runs" in reason for reason in facts.reasons))

    def test_malformed_homonymous_candidate_invalidates_green_candidate(self):
        malformed = CheckRunCandidate(
            result=None,
            name="ci-final-gate",
            error="candidate app slug is not github-actions",
        )
        facts = select_check_run_candidates(
            [self.candidate(101, 1, 201), malformed],
            ("ci-final-gate",),
            head_oid=COMMIT_A,
        )
        self.assertEqual(CiState.UNKNOWN, facts.ci_state)
        self.assertTrue(any("malformed candidate" in reason for reason in facts.reasons))

    def test_pending_and_failure_are_not_passing_evidence(self):
        pending = CheckRunCandidate(
            result=CheckResult(
                "check_run",
                "ci-final-gate",
                "IN_PROGRESS",
                None,
                CheckOutcome.PENDING,
                "https://github.com/owner/repo/actions/runs/201/job/9",
                check_run_id=101,
                head_sha=COMMIT_A,
                started_at="2026-09-05T10:00:00Z",
                completed_at=None,
                app_slug="github-actions",
                check_suite_id=1,
                workflow_run_id=201,
                workflow_id=77,
                workflow_path=".github/workflows/ci.yml",
                workflow_action="pull_request",
                workflow_event="pull_request",
                run_attempt=1,
            )
        )
        pending_facts = select_check_run_candidates(
            [pending], ("ci-final-gate",), head_oid=COMMIT_A
        )
        self.assertEqual(CiState.UNKNOWN, pending_facts.ci_state)
        self.assertTrue(any("pending" in reason for reason in pending_facts.reasons))
        failed = self.candidate(102, 1, 202, conclusion="FAILURE")
        failed_facts = select_check_run_candidates(
            [failed], ("ci-final-gate",), head_oid=COMMIT_A
        )
        self.assertEqual(CiState.BRANCH_FAILURE, failed_facts.ci_state)

    def test_push_name_is_not_an_authoritative_pr_result(self):
        push = self.candidate(100, 1, 200, name="ci-push-check")
        pr = self.candidate(101, 1, 201)
        facts = select_check_run_candidates(
            [push, pr],
            ("ci-final-gate",),
            head_oid=COMMIT_A,
        )
        self.assertEqual(CiState.PASSING, facts.ci_state)
        self.assertEqual(101, facts.checks[0].check_run_id)

    def test_snapshot_compares_event_sha_to_potential_merge_not_head(self):
        snapshot = LivePrSnapshot(
            pr_number=42,
            head_sha=COMMIT_A,
            base_ref_name="develop",
            potential_merge_commit_oid=COMMIT_B,
            body_sha256=hashlib.sha256(b"").hexdigest(),
            is_draft=False,
            event_name="pull_request",
            event_sha=COMMIT_B,
            workflow_path=".github/workflows/ci.yml",
            workflow_id=77,
            workflow_action="pull_request",
            run_id=201,
            run_attempt=1,
        )
        self.assertTrue(
            snapshot.matches_live(
                pr_number=42,
                head_sha=COMMIT_A,
                base_ref_name="develop",
                potential_merge_commit_oid=COMMIT_B,
                body="",
                is_draft=False,
                workflow_path=".github/workflows/ci.yml",
                workflow_id=77,
                workflow_action="pull_request",
                run_id=201,
                run_attempt=1,
            )
        )


class InfraEvidenceTests(TempDirTestCase):
    def test_require_infra_event_accepts_valid_event(self):
        evidence = require_infra_event(VALID_INFRA_EVENT)
        self.assertEqual("build", evidence.failed_job)
        self.assertEqual(("Run tests",), evidence.test_steps_not_started)
        self.assertEqual(COMMIT_A, evidence.commit)

    def test_require_infra_event_rejects_malformed(self):
        for event in ([], "INFRA", {"event_type": "INFRA_FAILURE"}):
            with self.subTest(event=event):
                with self.assertRaises(SourceMalformed):
                    require_infra_event(event)


class DiagnosticTests(TempDirTestCase):
    def test_stderr_appears_in_error_message(self):
        responses = self.base_responses()
        responses[self.git_c("worktree", "list", "--porcelain")] = (128, "", "fatal: worktree removed")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaisesRegex(SourceUnavailable, "fatal: worktree removed"):
            source.discover_worktree()

    def test_command_context_appears_in_error_message(self):
        responses = self.base_responses()
        responses[self.git_c("rev-parse", "HEAD")] = (128, "", "fatal: bad object")
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaisesRegex(SourceUnavailable, "rev-parse"):
            source.local_head()

    def test_secrets_redacted_from_error_message(self):
        responses = self.base_responses()
        responses[self.git_c("rev-parse", "HEAD")] = (
            1,
            "",
            "error: https://user:supersecret@example.com failed (token=supersecret)",
        )
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceUnavailable) as context:
            source.local_head()
        self.assertNotIn("supersecret", str(context.exception))

    def test_infra_event_type_must_match(self):
        event = dict(VALID_INFRA_EVENT, event_type="INFRA_RETRY")
        with self.assertRaises(SourceMalformed):
            require_infra_event(event)


class RedactionTests(TempDirTestCase):
    """T4R-N1 closure: common secret carriers are redacted from diagnostics,
    useful context survives, and non-secret prose controls stay untouched."""

    def test_authorization_bearer_header_redacted(self):
        out = redact("error: Authorization: Bearer ghp_SUPERSECRET rejected")
        self.assertNotIn("ghp_SUPERSECRET", out)
        self.assertIn("authorization: bearer ***", out.casefold())

    def test_authorization_bearer_redacts_all_alpha_secret(self):
        out = redact("denied: Authorization: Bearer supersecret")
        self.assertNotIn("supersecret", out)
        self.assertIn("***", out)

    def test_gh_token_colon_form_redacted(self):
        out = redact("error: GH_TOKEN: ghp_WHITESPACESECRET invalid")
        self.assertNotIn("ghp_WHITESPACESECRET", out)
        self.assertIn("GH_TOKEN: ***", out)

    def test_secret_key_colon_value_form_redacted(self):
        out = redact("error: API_KEY: abc123def rejected")
        self.assertNotIn("abc123def", out)
        self.assertIn("API_KEY: ***", out)

    def test_lowercase_key_colon_value_redacted(self):
        out = redact("error: token: supersecret rejected")
        self.assertNotIn("supersecret", out)
        self.assertIn("token: ***", out)

    def test_short_colon_value_redacted_regardless_of_length(self):
        out = redact("error: GH_TOKEN: ab rejected")
        self.assertNotIn("ab", out)
        self.assertIn("GH_TOKEN: ***", out)

    def test_mixed_case_key_colon_value_redacted(self):
        out = redact("error: Token: x rejected")
        self.assertNotIn("x", out)
        self.assertIn("Token: ***", out)

    def test_equals_separated_key_value_redacted(self):
        out = redact("error: token=ab rejected")
        self.assertNotIn("ab", out)
        self.assertIn("token=***", out)

    def test_secret_key_whitespace_separated_form_redacted(self):
        out = redact("error: GH_TOKEN ghp_SPACESECRET rejected")
        self.assertNotIn("ghp_SPACESECRET", out)
        self.assertIn("GH_TOKEN ***", out)

    def test_long_all_alpha_value_after_uppercase_key_redacted(self):
        out = redact("error: GH_TOKEN supersecret rejected")
        self.assertNotIn("supersecret", out)
        self.assertIn("GH_TOKEN ***", out)

    def test_secret_flag_value_redacted(self):
        out = redact("error: --token supersecret123 denied")
        self.assertNotIn("supersecret123", out)
        self.assertIn("--token ***", out)

    def test_short_flag_value_redacted_regardless_of_length(self):
        out = redact("error: --token ab12 denied")
        self.assertNotIn("ab12", out)
        self.assertIn("--token ***", out)

    def test_single_char_flag_value_redacted(self):
        out = redact("error: --password z denied")
        self.assertNotIn("z", out)
        self.assertIn("--password ***", out)

    def test_secret_flag_equals_form_redacted(self):
        out = redact("error: --api-key=xyz-123-abc denied")
        self.assertNotIn("xyz-123-abc", out)
        self.assertIn("--api-key=***", out)

    def test_equivalent_secret_flags_redacted(self):
        for flag in ("--password", "--secret", "--access-token", "--auth-token", "--gh-token"):
            with self.subTest(flag=flag):
                out = redact("error: {0} abc123def denied".format(flag))
                self.assertNotIn("abc123def", out)
                self.assertIn("***", out)

    def test_ssh_url_credentials_redacted(self):
        out = redact("fatal: unable to clone ssh://user:ghp_SSHSECRET@example.com/owner/repo")
        self.assertNotIn("ghp_SSHSECRET", out)
        self.assertIn("ssh://***@example.com", out)

    def test_https_url_credentials_still_redacted(self):
        out = redact("error: https://user:ghp_HTTPSECRET@example.com failed")
        self.assertNotIn("ghp_HTTPSECRET", out)
        self.assertIn("https://***@example.com", out)

    def test_key_equals_value_form_still_redacted(self):
        out = redact("error: (token=supersecret) denied")
        self.assertNotIn("supersecret", out)
        self.assertIn("token=***", out)

    def test_mixed_carriers_all_redacted_in_one_line(self):
        out = redact(
            "Authorization: Bearer ghp_A denied; GH_TOKEN: ghp_B invalid; "
            "--token ghp_C failed; ssh://u:ghp_D@h/x"
        )
        for secret in ("ghp_A", "ghp_B", "ghp_C", "ghp_D"):
            self.assertNotIn(secret, out)

    def test_bearer_prose_without_value_survives(self):
        out = redact("policy note: bearer policies apply to push")
        self.assertIn("bearer policies apply to push", out)

    def test_short_prose_values_survive(self):
        out = redact("docs: token is used for auth; note: token value appears in the README")
        self.assertIn("token is used for auth", out)
        self.assertIn("token value appears in the README", out)

    def test_lowercase_prose_key_values_survive(self):
        out = redact("note: token value appears in the README")
        self.assertIn("token value appears in the README", out)

    def test_flag_followed_by_prose_word_is_redacted(self):
        out = redact("usage: --token is documented")
        self.assertIn("--token ***", out)
        self.assertNotIn("--token is", out)

    def test_secret_flag_without_value_survives(self):
        out = redact("usage: --token")
        self.assertEqual("usage: --token", out)

    def test_redaction_is_idempotent(self):
        once = redact("error: GH_TOKEN: ghp_WHITESPACESECRET invalid")
        twice = redact(once)
        self.assertEqual(once, twice)

    def test_command_context_redacted_through_error_message(self):
        responses = self.base_responses()
        responses[self.git_c("rev-parse", "HEAD")] = (
            1,
            "",
            "error: GH_TOKEN: ghp_E2ESECRET rejected",
        )
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceUnavailable) as context:
            source.local_head()
        self.assertNotIn("ghp_E2ESECRET", str(context.exception))

    def test_short_and_all_alpha_secrets_never_reach_source_errors(self):
        responses = self.base_responses()
        responses[self.git_c("rev-parse", "HEAD")] = (
            1,
            "",
            "error: token: supersecret rejected; --token ab12 denied",
        )
        source = GitSource(self.wt, self.branch, runner=RecordingRunner(responses))
        with self.assertRaises(SourceUnavailable) as context:
            source.local_head()
        self.assertNotIn("supersecret", str(context.exception))
        self.assertNotIn("ab12", str(context.exception))

    def test_existing_url_and_kv_cases_remain_pinned(self):
        out = redact(
            "error: https://user:supersecret@example.com failed (token=supersecret)"
        )
        self.assertNotIn("supersecret", out)


class RealGitIntegrationTests(unittest.TestCase):
    """Real-git coverage for default_runner.

    The process CWD is moved to an unrelated directory before every read so the
    ambient CWD can never be what the discovery binds against, and all
    temporary repositories are removed with the CWD restored even on failure.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._cwd = os.getcwd()

        def cleanup():
            os.chdir(self._cwd)
            shutil.rmtree(self.tmp, ignore_errors=True)

        self.addCleanup(cleanup)

    def run_git(self, cwd, *args):
        proc = subprocess.run(("git", "-C", cwd) + args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(
                "git {0} failed with {1}: {2}".format(" ".join(args), proc.returncode, proc.stderr)
            )
        return proc

    def move_to_unrelated_cwd(self):
        unrelated = os.path.join(self.tmp, "unrelated")
        os.makedirs(unrelated)
        os.chdir(unrelated)

    def test_discover_and_facts_bind_with_default_runner_from_unrelated_cwd(self):
        origin = os.path.join(self.tmp, "origin.git")
        self.run_git(self.tmp, "init", "--bare", "--initial-branch", "main", "origin.git")
        repo = os.path.join(self.tmp, "main")
        self.run_git(self.tmp, "init", "--initial-branch", "main", "main")
        self.run_git(repo, "config", "user.email", "test@example.com")
        self.run_git(repo, "config", "user.name", "Test")
        with open(os.path.join(repo, "init.txt"), "w") as handle:
            handle.write("init\n")
        self.run_git(repo, "add", "init.txt")
        self.run_git(repo, "commit", "-m", "init")
        commit = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
        wt = os.path.join(self.tmp, "worktree")
        self.run_git(repo, "worktree", "add", "-b", "feature/close", wt)
        self.run_git(repo, "remote", "add", "origin", origin)
        self.run_git(repo, "push", "-u", "origin", "feature/close")

        self.move_to_unrelated_cwd()
        source = GitSource(wt, "feature/close", runner=default_runner)
        record = source.discover_worktree()
        self.assertEqual(os.path.realpath(wt), record.path)
        self.assertEqual("feature/close", record.branch)
        self.assertFalse(record.bare)
        facts = source.facts()
        self.assertEqual(commit, facts.local_commit)
        self.assertEqual(commit, facts.remote_commit)
        self.assertTrue(facts.worktree_clean)

    def test_bare_main_with_linked_worktree_reads_with_default_runner(self):
        bare = os.path.join(self.tmp, "bare.git")
        self.run_git(self.tmp, "init", "--bare", "--initial-branch", "main", "bare.git")
        seed = os.path.join(self.tmp, "seed")
        self.run_git(self.tmp, "clone", bare, "seed")
        self.run_git(seed, "config", "user.email", "test@example.com")
        self.run_git(seed, "config", "user.name", "Test")
        with open(os.path.join(seed, "init.txt"), "w") as handle:
            handle.write("init\n")
        self.run_git(seed, "add", "init.txt")
        self.run_git(seed, "commit", "-m", "init")
        self.run_git(seed, "push", "origin", "main")
        commit = self.run_git(seed, "rev-parse", "main").stdout.strip()
        wt = os.path.join(self.tmp, "worktree")
        self.run_git(bare, "worktree", "add", "-b", "feature/close", wt)
        self.run_git(wt, "remote", "add", "origin", bare)
        self.run_git(wt, "push", "-u", "origin", "feature/close")

        self.move_to_unrelated_cwd()
        source = GitSource(wt, "feature/close", runner=default_runner)
        record = source.discover_worktree()
        self.assertEqual(os.path.realpath(wt), record.path)
        self.assertEqual("feature/close", record.branch)
        self.assertFalse(record.bare)
        facts = source.facts()
        self.assertEqual(commit, facts.local_commit)
        self.assertEqual(commit, facts.remote_commit)
        self.assertTrue(facts.worktree_clean)


if __name__ == "__main__":
    unittest.main()
