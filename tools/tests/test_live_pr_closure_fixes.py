import base64
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from pr_closure.sources import (
    CiState,
    GitHubSource,
    PullRequestFacts,
    SourceMalformed,
    SourceUnavailable,
    _parse_json_object,
)
from pr_closure.cli import _assert_git_binding_unchanged, _assert_live_pr_unchanged


HEAD_A = "a" * 40
HEAD_B = "b" * 40
MERGE_A = "c" * 40
MERGE_B = "d" * 40


class Runner:
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
        if entry is None and "/actions/runs/" in key and "/attempts/" not in key and "/artifacts" not in key:
            run_id = key.rsplit("/actions/runs/", 1)[1]
            attempt_entries = []
            for candidate_key, candidate_entry in self.responses.items():
                prefix = "gh api repos/owner/repo/actions/runs/{0}/attempts/".format(run_id)
                if candidate_key.startswith(prefix) and isinstance(candidate_entry, tuple):
                    attempt_entries.append((int(candidate_key.rsplit("/", 1)[1]), candidate_entry))
            if attempt_entries:
                entry = max(attempt_entries, key=lambda item: item[0])[1]
        return entry or (127, "", "unscripted")


def pr(head=HEAD_A, merge=MERGE_A, body="body", draft=False):
    return PullRequestFacts(
        repository="owner/repo",
        number=42,
        head_branch="feature/close",
        base_ref_name="main",
        head_oid=head,
        is_draft=draft,
        state="OPEN",
        merge_state_status="CLEAN",
        mergeable="MERGEABLE",
        url="https://github.com/owner/repo/pull/42",
        checks=(),
        body=body,
        potential_merge_commit_oid=merge,
    )


def check(name, run_id=201, check_id=101, suite_id=301, start="2026-09-07T10:00:00Z"):
    return {
        "id": check_id,
        "name": name,
        "head_sha": HEAD_A,
        "status": "completed",
        "conclusion": "success",
        "started_at": start,
        "completed_at": "2026-09-07T10:01:00Z",
        "details_url": f"https://github.com/owner/repo/actions/runs/{run_id}/job/9",
        "app": {"slug": "github-actions"},
        "check_suite": {"id": suite_id},
    }


def run(run_id=201, suite_id=301, attempt=1, path=".github/workflows/ci.yml@main"):
    return {
        "id": run_id,
        "workflow_id": 77,
        "path": path,
        "event": "pull_request",
        "run_attempt": attempt,
        "head_sha": HEAD_A,
        "check_suite_id": suite_id,
    }


def snapshot(attempt=1, body="body", event_sha=MERGE_A):
    return {
        "pr_number": 42,
        "head_sha": HEAD_A,
        "base_ref_name": "main",
        "potential_merge_commit_oid": MERGE_A,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "is_draft": False,
        "event_name": "pull_request",
        "event_sha": event_sha,
        "workflow_path": ".github/workflows/ci.yml",
        "workflow_id": 77,
        "workflow_action": "pull_request",
        "run_id": 201,
        "run_attempt": attempt,
    }


class LivePrFixTests(unittest.TestCase):
    def live_source(self, raw_checks, snapshot_record=None):
        check_key = "gh api --paginate --slurp repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all&per_page=100"
        responses = {
            check_key: (0, json.dumps([{"total_count": len(raw_checks), "check_runs": raw_checks}]), ""),
            "gh api repos/owner/repo/actions/workflows/ci.yml": (
                0, json.dumps({"id": 77, "path": ".github/workflows/ci.yml"}), ""
            ),
        }
        for raw in raw_checks:
            run_id = int(raw["details_url"].split("/runs/")[1].split("/", 1)[0])
            raw["check_suite"]["id"] = 300 + (run_id - 200)
            suite_id = raw["check_suite"]["id"]
            responses[f"gh api repos/owner/repo/actions/runs/{run_id}"] = (0, json.dumps(run(run_id, suite_id=suite_id)), "")
            responses[f"gh api repos/owner/repo/actions/runs/{run_id}/attempts/1"] = (0, json.dumps(run(run_id, suite_id=suite_id)), "")
        responses[check_key] = (
            0, json.dumps([{"total_count": len(raw_checks), "check_runs": raw_checks}]), ""
        )
        selected = raw_checks[-1]
        run_id = int(selected["details_url"].split("/runs/")[1].split("/", 1)[0])
        responses[f"gh api --paginate --slurp repos/owner/repo/actions/runs/{run_id}/artifacts?per_page=100"] = (
            0, json.dumps([{"total_count": 1, "artifacts": [{"id": 501, "name": f"ci-pr-snapshot-{run_id}-1", "expired": False, "size_in_bytes": 512}]}]), ""
        )
        return GitHubSource("owner/repo", 42, runner=Runner(responses), artifact_reader=lambda *_: snapshot_record)

    def test_api_uses_documented_gh_argv_and_qualified_workflow_path(self):
        workflow_list = (
            "gh api repos/owner/repo/actions/workflows/ci.yml",
            (0, json.dumps({"id": 77, "path": ".github/workflows/ci.yml"}), ""),
        )
        runner = Runner(dict([workflow_list]))
        source = GitHubSource("owner/repo", 42, runner=runner)
        self.assertEqual(77, source._workflow_id(".github/workflows/ci.yml"))
        self.assertNotIn("--repo", runner.calls[0])

    def test_workflow_direct_endpoint_rejects_returned_path_mismatch(self):
        runner = Runner({
            "gh api repos/owner/repo/actions/workflows/ci.yml": (
                0, json.dumps({"id": 77, "path": ".github/workflows/other.yml"}), ""
            ),
        })
        with self.assertRaises((SourceMalformed, SourceUnavailable)) as caught:
            GitHubSource("owner/repo", 42, runner=runner)._workflow_id(
                ".github/workflows/ci.yml"
            )
        self.assertIsInstance(caught.exception, SourceMalformed)

    def test_workflow_direct_endpoint_rejects_page_one_when_total_count_exceeds_page(self):
        runner = Runner({
            "gh api repos/owner/repo/actions/workflows/ci.yml": (
                0, json.dumps({"id": 77, "path": ".github/workflows/other.yml"}), ""
            ),
            "gh api repos/owner/repo/actions/workflows?page=1&per_page=100": (
                0,
                json.dumps({
                    "total_count": 101,
                    "workflows": [{"id": 77, "path": ".github/workflows/ci.yml"}],
                }),
                "",
            ),
        })
        with self.assertRaises((SourceMalformed, SourceUnavailable)) as caught:
            GitHubSource("owner/repo", 42, runner=runner)._workflow_id(
                ".github/workflows/ci.yml"
            )
        self.assertIsInstance(caught.exception, SourceMalformed)
        self.assertEqual(
            ["repos/owner/repo/actions/workflows/ci.yml"],
            [call[-1] for call in runner.calls],
        )

    def test_workflow_direct_endpoint_rejects_non_numeric_id(self):
        runner = Runner({
            "gh api repos/owner/repo/actions/workflows/ci.yml": (
                0, json.dumps({"id": "77", "path": ".github/workflows/ci.yml"}), ""
            ),
        })
        with self.assertRaises((SourceMalformed, SourceUnavailable)) as caught:
            GitHubSource("owner/repo", 42, runner=runner)._workflow_id(
                ".github/workflows/ci.yml"
            )
        self.assertIsInstance(caught.exception, SourceMalformed)

    def test_provider_faithful_workflow_run_attempt_resolves_exact_check_suite(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        candidate = check("ci-final-gate", suite_id=301)
        responses = {
            "gh api " + endpoint + "&page=1&per_page=100": (
                0, json.dumps({"total_count": 1, "check_runs": [candidate]}), ""
            ),
            "gh api repos/owner/repo/actions/runs/201": (
                0, json.dumps(run(attempt=2, suite_id=302)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(run(attempt=1, suite_id=301)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/2": (
                0, json.dumps(run(attempt=2, suite_id=302)), ""
            ),
        }
        candidates = GitHubSource("owner/repo", 42, runner=Runner(responses)).read_check_run_candidates(
            HEAD_A,
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
            required_names=("ci-final-gate",),
        )
        self.assertEqual(1, len(candidates))
        self.assertIsNotNone(candidates[0].result)
        self.assertEqual(1, candidates[0].result.run_attempt)

    def test_provider_faithful_workflow_run_attempt_rejects_zero_or_multiple_suite_matches(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        for suite_ids in ((999, 302), (301, 301)):
            with self.subTest(suite_ids=suite_ids):
                candidate = check("ci-final-gate", suite_id=301)
                responses = {
                    "gh api " + endpoint + "&page=1&per_page=100": (
                        0, json.dumps({"total_count": 1, "check_runs": [candidate]}), ""
                    ),
                    "gh api repos/owner/repo/actions/runs/201": (
                        0, json.dumps(run(attempt=2, suite_id=suite_ids[1])), ""
                    ),
                    "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                        0, json.dumps(run(attempt=1, suite_id=suite_ids[0])), ""
                    ),
                    "gh api repos/owner/repo/actions/runs/201/attempts/2": (
                        0, json.dumps(run(attempt=2, suite_id=suite_ids[1])), ""
                    ),
                }
                candidates = GitHubSource("owner/repo", 42, runner=Runner(responses)).read_check_run_candidates(
                    HEAD_A,
                    workflow_path=".github/workflows/ci.yml",
                    workflow_action="pull_request",
                    required_names=("ci-final-gate",),
                )
                self.assertIsNone(candidates[0].result)
                self.assertIn("check-suite", candidates[0].error)

    def test_provider_faithful_current_run_suite_must_match_current_attempt(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        candidate = check("ci-final-gate", suite_id=301)
        responses = {
            "gh api " + endpoint + "&page=1&per_page=100": (
                0, json.dumps({"total_count": 1, "check_runs": [candidate]}), ""
            ),
            "gh api repos/owner/repo/actions/runs/201": (
                0, json.dumps(run(attempt=2, suite_id=999)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(run(attempt=1, suite_id=301)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/2": (
                0, json.dumps(run(attempt=2, suite_id=302)), ""
            ),
        }
        candidates = GitHubSource("owner/repo", 42, runner=Runner(responses)).read_check_run_candidates(
            HEAD_A,
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
            required_names=("ci-final-gate",),
        )
        self.assertIsNone(candidates[0].result)
        self.assertIn("current workflow attempt", candidates[0].error)

    def test_attempt_head_sha_must_bind_to_pr_head(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        candidate = check("ci-final-gate", suite_id=301)
        attempt = run(attempt=1, suite_id=301)
        attempt["head_sha"] = HEAD_B
        responses = {
            "gh api " + endpoint + "&page=1&per_page=100": (
                0, json.dumps({"total_count": 1, "check_runs": [candidate]}), ""
            ),
            "gh api repos/owner/repo/actions/runs/201": (
                0, json.dumps(run(attempt=1, suite_id=301)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(attempt), ""
            ),
        }
        candidates = GitHubSource("owner/repo", 42, runner=Runner(responses)).read_check_run_candidates(
            HEAD_A,
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
            required_names=("ci-final-gate",),
        )
        self.assertIsNone(candidates[0].result)
        self.assertIn("attempt head", candidates[0].error)

    def test_details_url_requires_expected_repo_and_exact_actions_job_shape(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        for details_url in (
            "https://github.com/other/repo/actions/runs/201/job/9",
            "https://github.com/owner/repo/actions/runs/201",
            "https://github.com/owner/repo/actions/runs/201/jobs/9",
            "https://github.com/owner/repo/actions/runs/0/job/9",
        ):
            with self.subTest(details_url=details_url):
                candidate = check("ci-final-gate", suite_id=301)
                candidate["details_url"] = details_url
                runner = Runner({
                    "gh api " + endpoint + "&page=1&per_page=100": (
                        0, json.dumps({"total_count": 1, "check_runs": [candidate]}), ""
                    ),
                })
                candidates = GitHubSource("owner/repo", 42, runner=runner).read_check_run_candidates(
                    HEAD_A,
                    workflow_path=".github/workflows/ci.yml",
                    workflow_action="pull_request",
                    required_names=("ci-final-gate",),
                )
                self.assertIsNone(candidates[0].result)
                self.assertTrue(
                    "details_url" in candidates[0].error
                    or "workflow run id" in candidates[0].error
                )

    def test_artifact_expired_requires_exact_false_boolean(self):
        source = GitHubSource(
            "owner/repo", 42, runner=Runner({}), artifact_reader=lambda *_: snapshot()
        )
        for expired in (None, True, "false", 0, 1):
            with self.subTest(expired=expired):
                metadata = {
                    "id": 501,
                    "name": "ci-pr-snapshot-201-1",
                    "size_in_bytes": 256,
                }
                if expired is not None:
                    metadata["expired"] = expired
                source._read_run_artifacts = lambda _run_id, metadata=metadata: (metadata,)
                with self.assertRaises(SourceMalformed):
                    source._read_snapshot(201, 1)

        source._read_run_artifacts = lambda _run_id: ({
            "id": 501,
            "name": "ci-pr-snapshot-201-1",
            "expired": False,
            "size_in_bytes": 256,
        },)
        record = source._read_snapshot(201, 1)
        self.assertEqual(42, record.pr_number)
        self.assertEqual(1, record.run_attempt)

    def test_real_gh_help_has_only_supported_pagination_flags(self):
        result = subprocess.run(
            ["gh", "api", "--help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--paginate", result.stdout)
        self.assertNotIn("--slurp", result.stdout)
        self.assertNotIn("--output", result.stdout)

    def test_explicit_page_traversal_uses_integer_pages_and_complete_count(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        page_one = "gh api " + endpoint + "&page=1&per_page=100"
        page_two = "gh api " + endpoint + "&page=2&per_page=100"
        raw_one = check("ci-final-gate", check_id=101)
        raw_two = check("other-gate", check_id=102)
        responses = {
            page_one: (0, json.dumps({"total_count": 2, "check_runs": [raw_one]}), ""),
            page_two: (0, json.dumps({"total_count": 2, "check_runs": [raw_two]}), ""),
            "gh api repos/owner/repo/actions/runs/201": (
                0, json.dumps(run(attempt=1, suite_id=301)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(run(attempt=1, suite_id=301)), ""
            ),
        }
        runner = Runner(responses)
        candidates = GitHubSource("owner/repo", 42, runner=runner).read_check_run_candidates(
            HEAD_A,
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
            required_names=("ci-final-gate",),
        )
        self.assertEqual(("ci-final-gate",), tuple(item.name for item in candidates))
        self.assertEqual(2, len([call for call in runner.calls if "check-runs" in call[-1]]))
        self.assertTrue(all("--slurp" not in call for call in runner.calls))

    def test_artifact_download_streams_exact_id_without_text_or_output_flag(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("snapshot.json", b'{"ok": true}')
        payload = archive.getvalue()
        observed = {}

        def artifact_runner(argv, timeout, output_path, limit):
            observed["argv"] = tuple(argv)
            observed["timeout"] = timeout
            observed["limit"] = limit
            output_path.write(payload)
            return 0, ""

        source = GitHubSource(
            "owner/repo", 42, artifact_runner=artifact_runner
        )
        self.assertEqual(
            {"ok": True},
            source._download_snapshot_record(
                501,
                "ci-pr-snapshot-201-1",
                expected_size=len(payload),
                expected_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
            ),
        )
        self.assertEqual(
            ("gh", "api", "repos/owner/repo/actions/artifacts/501/zip"),
            observed["argv"],
        )
        self.assertNotIn("--output", observed["argv"])
        self.assertEqual(64 * 1024, observed["limit"])

    def test_same_run_historical_candidate_is_bound_to_its_own_attempt(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        old = check("ci-final-gate", check_id=101, suite_id=301, start="2026-09-07T10:00:00Z")
        current = check(
            "ci-final-gate", check_id=102, suite_id=302, start="2026-09-07T11:00:00Z"
        )
        current["status"] = "in_progress"
        current["conclusion"] = None
        responses = {
            "gh api " + endpoint + "&page=1&per_page=100": (
                0, json.dumps({"total_count": 2, "check_runs": [old, current]}), ""
            ),
            "gh api repos/owner/repo/actions/runs/201": (
                0, json.dumps(run(attempt=2, suite_id=302)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(run(attempt=1, suite_id=301)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/2": (
                0, json.dumps(run(attempt=2, suite_id=302)), ""
            ),
        }
        candidates = GitHubSource("owner/repo", 42, runner=Runner(responses)).read_check_run_candidates(
            HEAD_A,
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
            required_names=("ci-final-gate",),
        )
        self.assertEqual({1, 2}, {candidate.result.run_attempt for candidate in candidates})
        self.assertTrue(all(candidate.result is not None for candidate in candidates))

    def test_full_reader_same_run_attempt_sequence_uses_current_attempt(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        for status, conclusion, expected in (
            ("in_progress", None, CiState.UNKNOWN),
            ("completed", "failure", CiState.BRANCH_FAILURE),
            ("completed", "success", CiState.PASSING),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                old = check("ci-final-gate", check_id=101, suite_id=301, start="2026-09-07T10:00:00Z")
                current = check("ci-final-gate", check_id=102, suite_id=302, start="2026-09-07T11:00:00Z")
                current["status"] = status
                current["conclusion"] = conclusion
                responses = {
                    "gh api repos/owner/repo/actions/workflows/ci.yml": (
                        0, json.dumps({"id": 77, "path": ".github/workflows/ci.yml"}), ""
                    ),
                    "gh api " + endpoint + "&page=1&per_page=100": (
                        0, json.dumps({"total_count": 2, "check_runs": [old, current]}), ""
                    ),
                    "gh api repos/owner/repo/actions/runs/201": (
                        0, json.dumps(run(attempt=2, suite_id=302)), ""
                    ),
                    "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                        0, json.dumps(run(attempt=1, suite_id=301)), ""
                    ),
                    "gh api repos/owner/repo/actions/runs/201/attempts/2": (
                        0, json.dumps(run(attempt=2, suite_id=302)), ""
                    ),
                }
                source = GitHubSource(
                    "owner/repo", 42, runner=Runner(responses),
                    artifact_reader=lambda *_: snapshot(attempt=2),
                )
                source._read_run_artifacts = lambda _run_id: ({
                    "id": 501,
                    "name": "ci-pr-snapshot-201-2",
                    "expired": False,
                    "size_in_bytes": 256,
                },)
                facts = source.read_live_ci(
                    pr(),
                    required_checks=("ci-final-gate",),
                    live_checks=("ci-final-gate",),
                    workflow_path=".github/workflows/ci.yml",
                    workflow_action="pull_request",
                )
                self.assertEqual(expected, facts.ci_state)
                self.assertEqual(102, facts.check_run_id)
                self.assertEqual(2, facts.run_attempt)

    def test_same_run_candidates_reuse_suite_and_attempt_reads(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        first = check("ci-final-gate", check_id=101, suite_id=301)
        second = check("ci-companion", check_id=102, suite_id=301)
        responses = {
            "gh api " + endpoint + "&page=1&per_page=100": (
                0, json.dumps({"total_count": 2, "check_runs": [first, second]}), ""
            ),
            "gh api repos/owner/repo/actions/runs/201": (
                0, json.dumps(run(attempt=1, suite_id=301)), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(run(attempt=1, suite_id=301)), ""
            ),
        }
        runner = Runner(responses)
        GitHubSource("owner/repo", 42, runner=runner).read_check_run_candidates(
            HEAD_A,
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
            required_names=("ci-final-gate", "ci-companion"),
        )
        self.assertEqual(
            1,
            sum(call[-1] == "repos/owner/repo/actions/runs/201" for call in runner.calls),
        )
        self.assertEqual(
            1,
            sum(call[-1] == "repos/owner/repo/actions/runs/201/attempts/1" for call in runner.calls),
        )

    def test_full_reader_rebinds_check_endpoint_to_new_head(self):
        raw = check("ci-final-gate", check_id=101)
        raw["head_sha"] = HEAD_B
        endpoint = "repos/owner/repo/commits/" + HEAD_B + "/check-runs?filter=all"
        workflow = run(suite_id=301)
        workflow["head_sha"] = HEAD_B
        responses = {
            "gh api repos/owner/repo/actions/workflows/ci.yml": (
                0, json.dumps({"id": 77, "path": ".github/workflows/ci.yml"}), ""
            ),
            "gh api " + endpoint + "&page=1&per_page=100": (
                0, json.dumps({"total_count": 1, "check_runs": [raw]}), ""
            ),
            "gh api repos/owner/repo/actions/runs/201": (
                0, json.dumps(workflow), ""
            ),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(workflow), ""
            ),
        }
        source = GitHubSource("owner/repo", 42, runner=Runner(responses))
        facts = source.read_live_ci(
            replace(pr(), head_oid=HEAD_B),
            required_checks=("ci-final-gate",),
            live_checks=(),
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
        )
        self.assertEqual(CiState.PASSING, facts.ci_state)

    def test_full_reader_accepts_fresh_base_and_merge_snapshot_only(self):
        live = replace(pr(), base_ref_name="release", potential_merge_commit_oid=MERGE_B)
        fresh = snapshot(event_sha=MERGE_B)
        fresh["base_ref_name"] = "release"
        fresh["potential_merge_commit_oid"] = MERGE_B
        source = self.live_source([check("ci-final-gate")], snapshot_record=fresh)
        facts = source.read_live_ci(
            live,
            required_checks=("ci-final-gate",),
            live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
        )
        self.assertEqual(CiState.PASSING, facts.ci_state)

    def test_foreign_same_name_central_candidate_is_rejected_before_selection(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        foreign = check("ci-final-gate", check_id=101, run_id=201, suite_id=301, start="2026-09-07T10:00:00Z")
        central = check("ci-final-gate", check_id=102, run_id=202, suite_id=302, start="2026-09-07T11:00:00Z")
        responses = {
            "gh api repos/owner/repo/actions/workflows/ci.yml": (
                0, json.dumps({"id": 77, "path": ".github/workflows/ci.yml"}), ""
            ),
            "gh api " + endpoint + "&page=1&per_page=100": (
                0, json.dumps({"total_count": 2, "check_runs": [foreign, central]}), ""
            ),
        }
        for item, workflow_id, path, suite, attempt in (
            (foreign, 88, ".github/workflows/foreign.yml@main", 301, 1),
            (central, 77, ".github/workflows/ci.yml@refs/pull/42/merge", 302, 1),
        ):
            run_id = int(item["details_url"].split("/runs/")[1].split("/", 1)[0])
            workflow = run(run_id, suite_id=suite, attempt=attempt, path=path)
            workflow["workflow_id"] = workflow_id
            responses[f"gh api repos/owner/repo/actions/runs/{run_id}"] = (
                0, json.dumps(workflow), ""
            )
            responses[f"gh api repos/owner/repo/actions/runs/{run_id}/attempts/{attempt}"] = (
                0, json.dumps(workflow), ""
            )
        source = GitHubSource("owner/repo", 42, runner=Runner(responses))
        facts = source.read_live_ci(
            pr(),
            required_checks=("ci-final-gate",),
            live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
        )
        self.assertEqual(CiState.UNKNOWN, facts.ci_state)
        self.assertTrue(any("malformed candidate" in reason for reason in facts.reasons))

    def test_newer_foreign_same_name_central_candidate_also_fails_closed(self):
        endpoint = "repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all"
        central = check("ci-final-gate", check_id=101, run_id=201, suite_id=301, start="2026-09-07T10:00:00Z")
        foreign = check("ci-final-gate", check_id=102, run_id=202, suite_id=302, start="2026-09-07T11:00:00Z")
        responses = {
            "gh api repos/owner/repo/actions/workflows/ci.yml": (
                0, json.dumps({"id": 77, "path": ".github/workflows/ci.yml"}), ""
            ),
            "gh api " + endpoint + "&page=1&per_page=100": (
                0, json.dumps({"total_count": 2, "check_runs": [central, foreign]}), ""
            ),
        }
        for item, workflow_id, path, suite in (
            (central, 77, ".github/workflows/ci.yml@refs/pull/42/merge", 301),
            (foreign, 88, ".github/workflows/foreign.yml@main", 302),
        ):
            run_id = int(item["details_url"].split("/runs/")[1].split("/", 1)[0])
            workflow = run(run_id, suite_id=suite, path=path)
            workflow["workflow_id"] = workflow_id
            responses[f"gh api repos/owner/repo/actions/runs/{run_id}"] = (
                0, json.dumps(workflow), ""
            )
            responses[f"gh api repos/owner/repo/actions/runs/{run_id}/attempts/1"] = (
                0, json.dumps(workflow), ""
            )
        facts = GitHubSource("owner/repo", 42, runner=Runner(responses)).read_live_ci(
            pr(),
            required_checks=("ci-final-gate",),
            live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
        )
        self.assertEqual(CiState.UNKNOWN, facts.ci_state)
        self.assertTrue(any("malformed candidate" in reason for reason in facts.reasons))

    def test_zip_requires_regular_member_when_mode_is_present(self):
        def archive_with_mode(mode):
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as handle:
                info = zipfile.ZipInfo("snapshot.json")
                info.external_attr = mode << 16
                handle.writestr(info, b'{"ok": true}')
            return output.getvalue()

        for mode, accepted in (
            (0, True),
            (stat.S_IFREG | 0o644, True),
            (stat.S_IFIFO | 0o644, False),
            (stat.S_IFCHR | 0o644, False),
            (stat.S_IFSOCK | 0o644, False),
        ):
            with self.subTest(mode=oct(mode)):
                payload = archive_with_mode(mode)

                def artifact_runner(argv, timeout, output_path, limit, payload=payload):
                    output_path.write(payload)
                    return 0, ""

                source = GitHubSource("owner/repo", 42, artifact_runner=artifact_runner)
                if accepted:
                    self.assertEqual(
                        {"ok": True},
                        source._download_snapshot_record(
                            501, "snapshot", expected_size=len(payload)
                        ),
                    )
                else:
                    with self.assertRaises(SourceMalformed):
                        source._download_snapshot_record(
                            501, "snapshot", expected_size=len(payload)
                        )

    def test_strict_json_rejects_nested_duplicate_keys(self):
        with self.assertRaises(SourceMalformed):
            _parse_json_object('{"page": {"id": 1, "id": 2}}', (), "fixture")

    def test_check_run_suite_and_attempt_are_bound(self):
        check_key = "gh api --paginate --slurp repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all&per_page=100"
        run_key = "gh api repos/owner/repo/actions/runs/201"
        attempt_key = "gh api repos/owner/repo/actions/runs/201/attempts/2"
        raw = check("ci-final-gate", suite_id=301)
        responses = {
            check_key: (0, json.dumps([{"total_count": 1, "check_runs": [raw]}]), ""),
            run_key: (0, json.dumps(run(attempt=2)), ""),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(run(attempt=1, suite_id=302)), ""
            ),
            attempt_key: (0, json.dumps(run(attempt=2, suite_id=301)), ""),
        }
        candidates = GitHubSource("owner/repo", 42, runner=Runner(responses)).read_check_run_candidates(
            HEAD_A,
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
            required_names=("ci-final-gate",),
        )
        self.assertEqual(2, candidates[0].result.run_attempt)

    def test_foreign_suite_and_attempt_are_unverified(self):
        check_key = "gh api --paginate --slurp repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all&per_page=100"
        run_key = "gh api repos/owner/repo/actions/runs/201"
        attempt_key = "gh api repos/owner/repo/actions/runs/201/attempts/2"
        raw = check("ci-final-gate", suite_id=301)
        responses = {
            check_key: (0, json.dumps([{"total_count": 1, "check_runs": [raw]}]), ""),
            run_key: (0, json.dumps(run(attempt=2, suite_id=999)), ""),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (
                0, json.dumps(run(attempt=1, suite_id=998)), ""
            ),
            attempt_key: (0, json.dumps(run(attempt=2, suite_id=999)), ""),
        }
        candidates = GitHubSource("owner/repo", 42, runner=Runner(responses)).read_check_run_candidates(
            HEAD_A, workflow_path=".github/workflows/ci.yml", workflow_action="pull_request",
            required_names=("ci-final-gate",),
        )
        self.assertIsNone(candidates[0].result)
        self.assertIn("check-suite", candidates[0].error)

    def test_pr_a_legacy_checks_and_central_live_gate_can_pass(self):
        checks = [check(name, check_id=100 + index) for index, name in enumerate((
            "front-e2e-gate", "front-ci-gate", "openapi-spec-drift-gate",
            "docs-archive-gate", "quality-gate", "react-doctor-gate", "ci-final-gate",
        ))]
        # The six legacy jobs have their own workflow/run provenance; only the
        # central gate is required to match the configured central workflow.
        for index, item in enumerate(checks):
            item["check_suite"]["id"] = 301 + index
        for index, item in enumerate(checks[:6]):
            item["details_url"] = f"https://github.com/owner/repo/actions/runs/{300 + index}/job/9"
        responses = {
            "gh api --paginate --slurp repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all&per_page=100": (
                0, json.dumps([{"total_count": 7, "check_runs": checks}]), ""
            ),
        }
        for index in range(6):
            responses[f"gh api repos/owner/repo/actions/runs/{300 + index}"] = (0, json.dumps(run(300 + index, suite_id=301 + index, path=f".github/workflows/legacy-{index}.yml@main")), "")
            responses[f"gh api repos/owner/repo/actions/runs/{300 + index}/attempts/1"] = (0, json.dumps(run(300 + index, suite_id=301 + index, path=f".github/workflows/legacy-{index}.yml@main")), "")
        responses["gh api repos/owner/repo/actions/runs/201"] = (0, json.dumps(run(suite_id=307)), "")
        responses["gh api repos/owner/repo/actions/runs/201/attempts/1"] = (0, json.dumps(run(suite_id=307)), "")
        responses["gh api --paginate --slurp repos/owner/repo/actions/runs/201/artifacts?per_page=100"] = (
            0, json.dumps([{"total_count": 1, "artifacts": [{"id": 501, "name": "ci-pr-snapshot-201-1", "expired": False, "size_in_bytes": 256}]}]), ""
        )
        responses["gh api repos/owner/repo/actions/workflows/ci.yml"] = (0, json.dumps({"id": 77, "path": ".github/workflows/ci.yml"}), "")
        source = GitHubSource("owner/repo", 42, runner=Runner(responses), artifact_reader=lambda *_: snapshot())
        facts = source.read_live_ci(
            pr(),
            required_checks=tuple(item["name"] for item in checks),
            live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml",
            workflow_action="pull_request",
        )
        self.assertEqual(CiState.PASSING, facts.ci_state, facts.reasons)

    def test_full_reader_transition_matrix_selects_newest_green_pending_and_red(self):
        old_green = check("ci-final-gate", run_id=201, check_id=101, start="2026-09-07T09:00:00Z")
        new_pending = check("ci-final-gate", run_id=202, check_id=102, start="2026-09-07T10:00:00Z")
        new_pending.update({"status": "in_progress", "conclusion": None})
        pending = self.live_source([old_green, new_pending], snapshot_record=snapshot())
        pending_facts = pending.read_live_ci(
            pr(), required_checks=("ci-final-gate",), live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml", workflow_action="pull_request",
        )
        self.assertEqual(CiState.UNKNOWN, pending_facts.ci_state)

        new_red = check("ci-final-gate", run_id=202, check_id=102, start="2026-09-07T10:00:00Z")
        new_red["conclusion"] = "failure"
        red = self.live_source([old_green, new_red], snapshot_record=snapshot())
        red_facts = red.read_live_ci(
            pr(), required_checks=("ci-final-gate",), live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml", workflow_action="pull_request",
        )
        self.assertEqual(CiState.BRANCH_FAILURE, red_facts.ci_state)

        old_red = check("ci-final-gate", run_id=201, check_id=101, start="2026-09-07T09:00:00Z")
        old_red["conclusion"] = "failure"
        new_green = check("ci-final-gate", run_id=202, check_id=102, start="2026-09-07T10:00:00Z")
        green_snapshot = snapshot()
        green_snapshot["run_id"] = 202
        green = self.live_source([old_red, new_green], snapshot_record=green_snapshot)
        green_facts = green.read_live_ci(
            pr(), required_checks=("ci-final-gate",), live_checks=("ci-final-gate",),
            workflow_path=".github/workflows/ci.yml", workflow_action="pull_request",
        )
        self.assertEqual(CiState.PASSING, green_facts.ci_state)

    def test_fingerprint_matrix_rejects_body_draft_base_merge_and_event_mutations(self):
        cases = (
            (pr(body="edited"), snapshot()),
            (pr(draft=True), snapshot()),
            (replace(pr(), base_ref_name="release"), snapshot()),
            (replace(pr(), potential_merge_commit_oid=MERGE_B), snapshot()),
            (pr(), snapshot(event_sha=MERGE_B)),
        )
        for live, stale in cases:
            with self.subTest(live=live):
                source = self.live_source([check("ci-final-gate")], snapshot_record=stale)
                facts = source.read_live_ci(
                    live, required_checks=("ci-final-gate",), live_checks=("ci-final-gate",),
                    workflow_path=".github/workflows/ci.yml", workflow_action="pull_request",
                )
                self.assertEqual(CiState.UNKNOWN, facts.ci_state)

    def test_required_name_prefilter_bounds_workflow_calls_and_keeps_same_name_candidates(self):
        unrelated = [check(f"unrelated-{index}", run_id=400 + index, check_id=400 + index) for index in range(20)]
        target = check("ci-final-gate")
        key = "gh api --paginate --slurp repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all&per_page=100"
        responses = {
            key: (0, json.dumps([{"total_count": 21, "check_runs": [*unrelated, target]}]), ""),
            "gh api repos/owner/repo/actions/runs/201": (0, json.dumps(run()), ""),
            "gh api repos/owner/repo/actions/runs/201/attempts/1": (0, json.dumps(run()), ""),
        }
        runner = Runner(responses)
        candidates = GitHubSource("owner/repo", 42, runner=runner).read_check_run_candidates(
            HEAD_A, workflow_path=".github/workflows/ci.yml", workflow_action="pull_request",
            required_names=("ci-final-gate",),
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual(2, len([call for call in runner.calls if "/actions/runs/" in " ".join(call)]))

    def test_provider_error_is_not_converted_to_passing_evidence(self):
        key = "gh api --paginate --slurp repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all&per_page=100"
        runner = Runner({key: (429, "", "rate limited")})
        with self.assertRaises(Exception):
            GitHubSource("owner/repo", 42, runner=runner).read_check_run_candidates(
                HEAD_A, workflow_path=".github/workflows/ci.yml", workflow_action="pull_request",
                required_names=("ci-final-gate",),
            )

    def test_pagination_count_mismatch_is_rejected(self):
        key = "gh api --paginate --slurp repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all&per_page=100"
        runner = Runner({key: (0, json.dumps([{"total_count": 2, "check_runs": [check("ci-final-gate")]}]), "")})
        with self.assertRaises(SourceMalformed):
            GitHubSource("owner/repo", 42, runner=runner).read_check_run_candidates(
                HEAD_A, workflow_path=".github/workflows/ci.yml", workflow_action="pull_request",
                required_names=("ci-final-gate",),
            )

    def test_final_live_pr_reread_rejects_body_head_base_draft_or_merge_mutation(self):
        for changed in (
            {"body": "edited"},
            {"head_oid": HEAD_B},
            {"base_ref_name": "release"},
            {"potential_merge_commit_oid": MERGE_B},
            {"is_draft": True},
        ):
            with self.subTest(changed=changed):
                before = pr()
                after = replace(before, **changed)
                with self.assertRaises(SourceMalformed):
                    _assert_live_pr_unchanged(before, after)

    def test_final_git_reread_rejects_worktree_binding_mutation(self):
        from pr_closure.sources import GitFacts

        before = GitFacts("/tmp/w", "feature/close", "feature/close", HEAD_A, HEAD_A, True)
        after = GitFacts("/tmp/w", "feature/close", "feature/close", HEAD_B, HEAD_A, True)
        with self.assertRaises(SourceMalformed):
            _assert_git_binding_unchanged(before, after)

    def test_artifact_reader_receives_exact_validated_id_and_size_is_bounded(self):
        seen = []
        source = GitHubSource("owner/repo", 42, runner=Runner({}), artifact_reader=lambda artifact_id, _: (seen.append(artifact_id) or snapshot()))
        source._read_run_artifacts = lambda _run_id: ({
            "id": 501,
            "name": "ci-pr-snapshot-201-1",
            "expired": False,
            "size_in_bytes": 256,
        },)
        source._read_snapshot(201, 1)
        self.assertEqual([501], seen)

        source._read_run_artifacts = lambda _run_id: ({
            "id": 501,
            "name": "ci-pr-snapshot-201-1",
            "expired": False,
            "size_in_bytes": 65 * 1024,
        },)
        with self.assertRaises(SourceMalformed):
            source._read_snapshot(201, 1)

    def test_artifact_path_symlink_is_rejected(self):
        def artifact_runner(argv, _timeout, output, _limit):
            with zipfile.ZipFile(output, "w") as archive:
                entry = zipfile.ZipInfo("snapshot.json")
                entry.create_system = 3
                entry.external_attr = (0o120777 << 16) | 0xA000
                archive.writestr(entry, b"{}")
            return (0, "", "")

        source = GitHubSource("owner/repo", 42, artifact_runner=artifact_runner)
        with self.assertRaises(SourceMalformed):
            source._download_snapshot_record(501, "snapshot", expected_size=256)

    def test_artifact_pagination_rejects_replay_and_count_mismatch(self):
        source = GitHubSource("owner/repo", 42, runner=Runner({}))
        source._runner = Runner({
            "gh api --paginate --slurp repos/owner/repo/actions/runs/201/artifacts?per_page=100": (
                0, json.dumps([
                    {"total_count": 2, "artifacts": [{"id": 501, "name": "one"}]},
                    {"total_count": 2, "artifacts": [{"id": 501, "name": "one"}]},
                ]), ""
            )
        })
        with self.assertRaises(SourceMalformed):
            source._read_run_artifacts(201)

    def test_malformed_snapshot_json_is_rejected(self):
        source = GitHubSource("owner/repo", 42, runner=Runner({}), artifact_reader=lambda *_: b'{"event_sha": 1, "event_sha": 2}')
        source._read_run_artifacts = lambda _run_id: ({
            "id": 501, "name": "ci-pr-snapshot-201-1", "expired": False, "size_in_bytes": 64,
        },)
        with self.assertRaises(SourceMalformed):
            source._read_snapshot(201, 1)


if __name__ == "__main__":
    unittest.main()
