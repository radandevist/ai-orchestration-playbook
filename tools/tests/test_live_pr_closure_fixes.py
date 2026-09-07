import base64
import hashlib
import json
import os
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
        return self.responses.get(" ".join(argv), (127, "", "unscripted"))


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
            "gh api repos/owner/repo/actions/workflows?per_page=100": (
                0, json.dumps({"total_count": 1, "workflows": [{"id": 77, "path": ".github/workflows/ci.yml"}]}), ""
            ),
        }
        for raw in raw_checks:
            run_id = int(raw["details_url"].split("/runs/")[1].split("/", 1)[0])
            suite_id = raw["check_suite"]["id"]
            responses[f"gh api repos/owner/repo/actions/runs/{run_id}"] = (0, json.dumps(run(run_id, suite_id=suite_id)), "")
            responses[f"gh api repos/owner/repo/actions/runs/{run_id}/attempts/1"] = (0, json.dumps(run(run_id, suite_id=suite_id)), "")
        selected = raw_checks[-1]
        run_id = int(selected["details_url"].split("/runs/")[1].split("/", 1)[0])
        responses[f"gh api --paginate --slurp repos/owner/repo/actions/runs/{run_id}/artifacts?per_page=100"] = (
            0, json.dumps([{"total_count": 1, "artifacts": [{"id": 501, "name": f"ci-pr-snapshot-{run_id}-1", "expired": False, "size_in_bytes": 512}]}]), ""
        )
        return GitHubSource("owner/repo", 42, runner=Runner(responses), artifact_reader=lambda *_: snapshot_record)

    def test_api_uses_documented_gh_argv_and_qualified_workflow_path(self):
        workflow_list = (
            "gh api repos/owner/repo/actions/workflows?per_page=100",
            (0, json.dumps({"total_count": 1, "workflows": [{"id": 77, "path": ".github/workflows/ci.yml"}]}), ""),
        )
        runner = Runner(dict([workflow_list]))
        source = GitHubSource("owner/repo", 42, runner=runner)
        self.assertEqual(77, source._workflow_id(".github/workflows/ci.yml"))
        self.assertNotIn("--repo", runner.calls[0])

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
        for index, item in enumerate(checks[:6]):
            item["details_url"] = f"https://github.com/owner/repo/actions/runs/{300 + index}/job/9"
        responses = {
            "gh api --paginate --slurp repos/owner/repo/commits/" + HEAD_A + "/check-runs?filter=all&per_page=100": (
                0, json.dumps([{"total_count": 7, "check_runs": checks}]), ""
            ),
        }
        for index in range(6):
            responses[f"gh api repos/owner/repo/actions/runs/{300 + index}"] = (0, json.dumps(run(300 + index, path=f".github/workflows/legacy-{index}.yml@main")), "")
            responses[f"gh api repos/owner/repo/actions/runs/{300 + index}/attempts/1"] = (0, json.dumps(run(300 + index, suite_id=301, path=f".github/workflows/legacy-{index}.yml@main")), "")
        responses["gh api repos/owner/repo/actions/runs/201"] = (0, json.dumps(run()), "")
        responses["gh api repos/owner/repo/actions/runs/201/attempts/1"] = (0, json.dumps(run()), "")
        responses["gh api --paginate --slurp repos/owner/repo/actions/runs/201/artifacts?per_page=100"] = (
            0, json.dumps([{"total_count": 1, "artifacts": [{"id": 501, "name": "ci-pr-snapshot-201-1", "expired": False, "size_in_bytes": 256}]}]), ""
        )
        responses["gh api repos/owner/repo/actions/workflows?per_page=100"] = (0, json.dumps({"total_count": 1, "workflows": [{"id": 77, "path": ".github/workflows/ci.yml"}]}), "")
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
        def runner(argv, _timeout=None):
            output = argv[argv.index("--output") + 1]
            with zipfile.ZipFile(output, "w") as archive:
                entry = zipfile.ZipInfo("snapshot.json")
                entry.create_system = 3
                entry.external_attr = (0o120777 << 16) | 0xA000
                archive.writestr(entry, b"{}")
            return (0, "", "")

        source = GitHubSource("owner/repo", 42, runner=runner)
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
