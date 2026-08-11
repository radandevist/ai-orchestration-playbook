import importlib.metadata
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from pr_closure.contract import (
    CONFIG_FORBIDDEN_ROOT_SPECS,
    CONFIG_SEMANTIC_ASYMMETRIES,
    SEMANTIC_ASYMMETRIES,
    ConfigValidationError,
    project_json_schema,
    validate_project_config,
)
from pr_closure.review import ReviewValidationError, validate_review

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "review-record-v1.json"
CONFIG_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "project-closure-v1.json"
REQUIREMENTS_PATH = Path(__file__).resolve().parent / "requirements-test.txt"


def _schema():
    return json.loads(SCHEMA_PATH.read_text())


def _valid_record():
    return {
        "schema_version": 1,
        "repository": "owner/repo",
        "pr_number": 42,
        "reviewed_branch": "feat/pr-closure-framework",
        "reviewed_commit": "a" * 40,
        "base_commit": "b" * 40,
        "implementer_family": "deepseek",
        "reviewer_family": "claude",
        "local_evidence": ["tests:tools/tests/test_schema_agreement.py"],
        "ci_evidence": ["ci:pr-check/run-1"],
        "verdict": "APPROVED_WITH_FOLLOW_UPS",
        "findings": [{
            "id": "F-1",
            "root_cause": "missing-output-test",
            "severity": "MINOR",
            "disposition": "FOLLOW_UP_ISSUE",
            "scope": "pre_existing",
            "summary": "Operator output lacks a mutation guard",
            "evidence": ["476 tests survive the mutation"],
            "follow_up_issue": 900,
        }],
        "intentionally_not_findings": [],
    }


def _swap_range(record):
    record.pop("base_commit")
    record["comparison_range"] = "c236e90..5dcfc09"


def _blank_range(record):
    record.pop("base_commit")
    record["comparison_range"] = "   "


def _follow_up_issue_on(disposition):
    def mutate(record):
        record["verdict"] = "CHANGES_REQUIRED"
        record["findings"][0].update({
            "disposition": disposition,
            "follow_up_issue": None,
        })

    return mutate


def agreement_corpus():
    """(label, record, schema_valid, python_valid, asymmetry_id) tuples.

    Agreement and deliberate asymmetry are both first-class members; no assertion
    privileges either outcome. Every deliberate schema-accepts/Python-rejects case
    names the independently declared semantic asymmetry that explains it.
    """

    def add(label, mutate, schema_ok, python_ok, asymmetry_id=None):
        record = _valid_record()
        mutate(record)
        cases.append((label, record, schema_ok, python_ok, asymmetry_id))

    cases = []

    add("valid record", lambda r: None, True, True)
    add("large pr_number remains valid", lambda r: r.update({"pr_number": 100001}), True, True)

    add("unknown top-level key", lambda r: r.update({"rogue": "x"}), False, False)
    add(
        "unknown finding key",
        lambda r: r["findings"][0].update({"rogue": "x"}),
        False,
        False,
    )
    add(
        "null root_cause",
        lambda r: r["findings"][0].update({"root_cause": None}),
        False,
        False,
    )
    add("empty scope", lambda r: r["findings"][0].update({"scope": ""}), False, False)
    add(
        "int summary",
        lambda r: r["findings"][0].update({"summary": 123}),
        False,
        False,
    )
    add("repository a/b/c", lambda r: r.update({"repository": "a/b/c"}), False, False)
    add("unknown verdict", lambda r: r.update({"verdict": "LOOKS_FINE"}), False, False)
    add("bad commit", lambda r: r.update({"reviewed_commit": "NOPE"}), False, False)
    add(
        "follow_up_issue on NOTE_ONLY",
        lambda r: r["findings"][0].update({"disposition": "NOTE_ONLY"}),
        False,
        False,
    )
    add(
        "both base_commit and comparison_range",
        lambda r: r.update({"comparison_range": "c236e90..5dcfc09"}),
        False,
        False,
    )
    add(
        "neither base_commit nor comparison_range",
        lambda r: r.pop("base_commit"),
        False,
        False,
    )
    add("empty local_evidence", lambda r: r.update({"local_evidence": []}), False, False)
    add("empty ci_evidence", lambda r: r.update({"ci_evidence": []}), False, False)
    add("boolean pr_number", lambda r: r.update({"pr_number": True}), False, False)
    add(
        "boolean follow_up_issue",
        lambda r: r["findings"][0].update({"follow_up_issue": True}),
        False,
        False,
    )

    add(
        "blank summary",
        lambda r: r["findings"][0].update({"summary": "   "}),
        False,
        False,
    )
    add(
        "blank root_cause",
        lambda r: r["findings"][0].update({"root_cause": "   "}),
        False,
        False,
    )
    add("blank scope", lambda r: r["findings"][0].update({"scope": "   "}), False, False)
    add("blank finding id", lambda r: r["findings"][0].update({"id": "   "}), False, False)
    add("blank reviewed_branch", lambda r: r.update({"reviewed_branch": "   "}), False, False)
    add("blank comparison_range", _blank_range, False, False)
    add(
        "blank evidence item",
        lambda r: r["findings"][0].update({"evidence": ["ok", "   "]}),
        False,
        False,
    )
    add(
        "blank local_evidence item",
        lambda r: r.update({"local_evidence": ["ok", "   "]}),
        False,
        False,
    )
    add(
        "blank ci_evidence item",
        lambda r: r.update({"ci_evidence": ["ok", "   "]}),
        False,
        False,
    )
    add(
        "blank intentionally_not_findings item",
        lambda r: r.update({"intentionally_not_findings": ["ok", "   "]}),
        False,
        False,
    )
    add("blank implementer_family", lambda r: r.update({"implementer_family": "   "}), False, False)
    add("blank reviewer_family", lambda r: r.update({"reviewer_family": "   "}), False, False)

    add(
        "explicit null follow_up_issue on NOTE_ONLY",
        _follow_up_issue_on("NOTE_ONLY"),
        False,
        False,
    )
    add(
        "explicit null follow_up_issue on BLOCKS_PR",
        _follow_up_issue_on("BLOCKS_PR"),
        False,
        False,
    )
    add(
        "explicit null follow_up_issue on FOLLOW_UP_ISSUE",
        _follow_up_issue_on("FOLLOW_UP_ISSUE"),
        False,
        False,
    )

    add(
        "paired but empty proof arrays",
        lambda r: r["findings"][0].update({
            "bad_case_evidence": [],
            "good_case_evidence": [],
        }),
        False,
        False,
    )
    add(
        "one empty proof array",
        lambda r: r["findings"][0].update({
            "bad_case_evidence": ["escape.sh"],
            "good_case_evidence": [],
        }),
        False,
        False,
    )

    add(
        "reviewed_commit trailing newline",
        lambda r: r.update({"reviewed_commit": "a" * 40 + "\n"}),
        False,
        False,
    )
    add(
        "base_commit trailing newline",
        lambda r: r.update({"base_commit": "a" * 40 + "\n"}),
        False,
        False,
    )
    add(
        "repository trailing newline",
        lambda r: r.update({"repository": "owner/repo\n"}),
        False,
        False,
    )

    add(
        "duplicate finding IDs",
        lambda r: r["findings"].append(dict(r["findings"][0], severity="MAJOR")),
        True,
        False,
        "duplicate_finding_ids",
    )
    add(
        "unknown family wizard-9000",
        lambda r: r.update({"implementer_family": "wizard-9000"}),
        True,
        False,
        "unknown_model_family",
    )
    add(
        "third-party lookalike claude-killer",
        lambda r: r.update({"implementer_family": "claude-killer"}),
        True,
        False,
        "third_party_lookalike",
    )
    add(
        "same family under different spellings",
        lambda r: r.update({"reviewer_family": "deepseek-v4-flash"}),
        True,
        False,
        "same_model_family",
    )
    add(
        "schema_version 1.0",
        lambda r: r.update({"schema_version": 1.0}),
        True,
        False,
        "exact_integer_types",
    )
    add(
        "pr_number 42.0",
        lambda r: r.update({"pr_number": 42.0}),
        True,
        False,
        "exact_integer_types",
    )
    add(
        "follow_up_issue 900.0",
        lambda r: r["findings"][0].update({"follow_up_issue": 900.0}),
        True,
        False,
        "exact_integer_types",
    )

    return cases


class AgreementCorpusShapeTests(unittest.TestCase):
    def test_agreement_suite_is_not_silently_skippable(self):
        source = Path(__file__).read_text()
        skip_sentinel = "skip" + "Unless"
        import_guard = "except" + " ImportError"
        self.assertNotIn(skip_sentinel, source)
        self.assertNotIn(import_guard, source)
        self.assertIn("Draft202012Validator", source)

    def test_jsonschema_is_a_pinned_test_only_dependency(self):
        path = REQUIREMENTS_PATH
        self.assertTrue(path.is_file(), f"missing pinned test requirements file: {path}")
        pin = path.read_text().strip()
        self.assertRegex(pin, r"^jsonschema==\d+\.\d+\.\d+$")
        installed = importlib.metadata.version("jsonschema")
        self.assertEqual(pin.split("==", 1)[1], installed)

    def test_production_package_does_not_import_jsonschema(self):
        package_dir = Path(__file__).resolve().parent.parent / "pr_closure"
        offenders = []
        for py in sorted(package_dir.glob("*.py")):
            if "jsonschema" in py.read_text():
                offenders.append(str(py))
        self.assertEqual([], offenders)


class SchemaPythonDifferentialTests(unittest.TestCase):
    def test_every_corpus_case_matches_its_declared_outcome(self):
        validator = Draft202012Validator(_schema())
        mismatches = []
        for label, record, schema_ok, python_ok, _ in agreement_corpus():
            got_schema = validator.is_valid(record)
            try:
                validate_review(record)
                got_python = True
            except ReviewValidationError:
                got_python = False
            expected = (schema_ok, python_ok)
            got = (got_schema, got_python)
            if got != expected:
                mismatches.append(
                    f"{label}: got schema={got_schema} python={got_python} "
                    f"expected schema={schema_ok} python={python_ok}"
                )
        self.assertEqual([], mismatches)

    def test_corpus_declares_both_agreement_and_deliberate_asymmetry(self):
        outcomes = {row[2:4] for row in agreement_corpus()}
        self.assertTrue((True, True) in outcomes)
        self.assertTrue((False, False) in outcomes)
        self.assertTrue((True, False) in outcomes)
        self.assertNotIn((False, True), outcomes)

    def test_asymmetry_registry_is_complete_and_published(self):
        declared = {item.id: item.description for item in SEMANTIC_ASYMMETRIES}
        covered = set()
        errors = []
        for label, _, schema_ok, python_ok, asymmetry_id in agreement_corpus():
            is_deliberate_asymmetry = (schema_ok, python_ok) == (True, False)
            if is_deliberate_asymmetry and asymmetry_id is None:
                errors.append(f"{label}: missing asymmetry id")
            if not is_deliberate_asymmetry and asymmetry_id is not None:
                errors.append(f"{label}: unexpected asymmetry id {asymmetry_id!r}")
            if asymmetry_id is not None:
                covered.add(asymmetry_id)
        self.assertEqual([], errors)
        self.assertEqual(set(declared), covered)

        comment = _schema().get("$comment", "")
        for asymmetry_id, description in declared.items():
            with self.subTest(asymmetry_id=asymmetry_id):
                self.assertIn(f"{asymmetry_id}: {description}", comment)

    def test_explicit_null_follow_up_cases_isolate_the_key_presence_rule(self):
        isolated = [
            row
            for row in agreement_corpus()
            if row[0].startswith("explicit null follow_up_issue on")
        ]
        self.assertEqual(3, len(isolated))
        self.assertEqual(
            {"NOTE_ONLY", "BLOCKS_PR", "FOLLOW_UP_ISSUE"},
            {_disposition_of(row) for row in isolated},
        )

    def test_corpus_covers_each_structural_group(self):
        counts = {}
        for label, _, schema_ok, python_ok, _ in agreement_corpus():
            group = _group_of(label)
            counts[group] = counts.get(group, 0) + 1
        self.assertEqual(
            {
                "valid": 1,
                "boundary": 1,
                "structural": 15,
                "whitespace": 12,
                "null_follow_up": 3,
                "paired_empty": 2,
                "trailing_newline": 3,
                "asymmetric": 7,
            },
            counts,
        )


def _valid_config():
    return {
        "schema_version": 1,
        "project": "publyapp",
        "repository": "owner/repo",
        "repo_path": "/var/tmp/durable/repo",
        "default_branch": "develop",
        "closure_state_dir": "/var/tmp/durable/state",
        "local_review_ready_commands": ["npm run verify"],
        "closure_acceptance_commands": ["npm run acceptance"],
        "infra_retry_budget": 1,
        "stagnation_budget_minutes": 240,
        "heavy_job_limit": 1,
        "verification_command_timeout_seconds": 300,
        "tracking_projection": "trello card update",
    }


def config_agreement_corpus():
    """(label, config, schema_valid, python_valid, asymmetry_id) tuples for the
    project-closure-v1 contract. Agreement and deliberate asymmetry are both
    first-class members; every deliberate schema-accepts/Python-rejects case
    names the independently declared semantic asymmetry that explains it."""

    def add(label, mutate, schema_ok, python_ok, asymmetry_id=None):
        config = _valid_config()
        mutate(config)
        cases.append((label, config, schema_ok, python_ok, asymmetry_id))

    cases = []

    add("valid config", lambda c: None, True, True)
    add("null tracking_projection", lambda c: c.update({"tracking_projection": None}), True, True)
    add(
        "valid ci_required_checks",
        lambda c: c.update({"ci_required_checks": ["gate-a", "gate-b"]}),
        True,
        True,
    )
    add(
        "empty ci_required_checks legacy",
        lambda c: c.update({"ci_required_checks": []}),
        True,
        True,
    )
    add(
        "blank ci_required_checks item",
        lambda c: c.update({"ci_required_checks": ["gate-a", "  "]}),
        False,
        False,
    )
    add(
        "duplicate ci_required_checks",
        lambda c: c.update({"ci_required_checks": ["gate-a", "gate-a"]}),
        False,
        False,
    )
    add(
        "non-string ci_required_checks item",
        lambda c: c.update({"ci_required_checks": [42]}),
        False,
        False,
    )
    add(
        "string ci_required_checks",
        lambda c: c.update({"ci_required_checks": "gate-a"}),
        False,
        False,
    )

    add("unknown top-level key", lambda c: c.update({"rogue": "x"}), False, False)
    add("missing schema_version", lambda c: c.pop("schema_version"), False, False)
    add(
        "missing closure_acceptance_commands",
        lambda c: c.pop("closure_acceptance_commands"),
        False,
        False,
    )
    add("unsupported schema_version", lambda c: c.update({"schema_version": 2}), False, False)
    add("boolean schema_version", lambda c: c.update({"schema_version": True}), False, False)
    add(
        "boolean infra_retry_budget",
        lambda c: c.update({"infra_retry_budget": True}),
        False,
        False,
    )
    add("boolean heavy_job_limit", lambda c: c.update({"heavy_job_limit": True}), False, False)
    add(
        "float infra_retry_budget",
        lambda c: c.update({"infra_retry_budget": 1.5}),
        False,
        False,
    )
    add("zero infra_retry_budget", lambda c: c.update({"infra_retry_budget": 0}), False, False)
    add(
        "negative stagnation_budget_minutes",
        lambda c: c.update({"stagnation_budget_minutes": -5}),
        False,
        False,
    )
    add("heavy_job_limit two", lambda c: c.update({"heavy_job_limit": 2}), False, False)
    add("heavy_job_limit zero", lambda c: c.update({"heavy_job_limit": 0}), False, False)
    add(
        "missing verification_command_timeout_seconds",
        lambda c: c.pop("verification_command_timeout_seconds"),
        False,
        False,
    )
    add(
        "zero verification_command_timeout_seconds",
        lambda c: c.update({"verification_command_timeout_seconds": 0}),
        False,
        False,
    )
    add(
        "float verification_command_timeout_seconds",
        lambda c: c.update({"verification_command_timeout_seconds": 5.5}),
        False,
        False,
    )
    add("project with slash", lambda c: c.update({"project": "a/b"}), False, False)
    add("project with backslash", lambda c: c.update({"project": "a\\b"}), False, False)
    add(
        "project with surrounding whitespace",
        lambda c: c.update({"project": " spaced "}),
        False,
        False,
    )
    add("repository without slash", lambda c: c.update({"repository": "norepo"}), False, False)
    add(
        "repository with space",
        lambda c: c.update({"repository": "owner repo/x"}),
        False,
        False,
    )
    add("repository trailing slash", lambda c: c.update({"repository": "owner/"}), False, False)
    add(
        "repository trailing newline",
        lambda c: c.update({"repository": "owner/repo\n"}),
        False,
        False,
    )
    add("blank default_branch", lambda c: c.update({"default_branch": "   "}), False, False)
    add(
        "default_branch with space",
        lambda c: c.update({"default_branch": "branch name"}),
        False,
        False,
    )
    add("relative repo_path", lambda c: c.update({"repo_path": "relative/path"}), False, False)
    add("empty repo_path", lambda c: c.update({"repo_path": ""}), False, False)
    add(
        "relative closure_state_dir",
        lambda c: c.update({"closure_state_dir": "relative/state"}),
        False,
        False,
    )
    add(
        "empty local_review_ready_commands",
        lambda c: c.update({"local_review_ready_commands": []}),
        False,
        False,
    )
    add(
        "empty closure_acceptance_commands",
        lambda c: c.update({"closure_acceptance_commands": []}),
        False,
        False,
    )
    add(
        "blank command item",
        lambda c: c.update({"local_review_ready_commands": ["ok", "   "]}),
        False,
        False,
    )
    add(
        "non-string command",
        lambda c: c.update({"local_review_ready_commands": [42]}),
        False,
        False,
    )
    add(
        "NUL command string",
        lambda c: c.update({"local_review_ready_commands": ["echo \x00 boom"]}),
        True,
        False,
        "nul_command_string",
    )
    add("empty tracking_projection", lambda c: c.update({"tracking_projection": ""}), False, False)
    add(
        "blank tracking_projection",
        lambda c: c.update({"tracking_projection": "   "}),
        False,
        False,
    )
    add(
        "non-string tracking_projection",
        lambda c: c.update({"tracking_projection": 42}),
        False,
        False,
    )

    add(
        "repo_path under /tmp",
        lambda c: c.update({"repo_path": "/tmp/scratch/repo"}),
        True,
        False,
        "forbidden_temporary_paths",
    )
    add(
        "closure_state_dir under /tmp",
        lambda c: c.update({"closure_state_dir": "/tmp/scratch/state"}),
        True,
        False,
        "forbidden_temporary_paths",
    )
    add("project dot", lambda c: c.update({"project": "."}), True, False, "unsafe_project_component")
    add(
        "project dotdot",
        lambda c: c.update({"project": ".."}),
        True,
        False,
        "unsafe_project_component",
    )
    add(
        "project NUL byte",
        lambda c: c.update({"project": "a\x00b"}),
        True,
        False,
        "unsafe_project_component",
    )
    add(
        "schema_version 1.0",
        lambda c: c.update({"schema_version": 1.0}),
        True,
        False,
        "exact_integer_types",
    )
    add(
        "infra_retry_budget 1.0",
        lambda c: c.update({"infra_retry_budget": 1.0}),
        True,
        False,
        "exact_integer_types",
    )
    add(
        "heavy_job_limit 1.0",
        lambda c: c.update({"heavy_job_limit": 1.0}),
        True,
        False,
        "exact_integer_types",
    )

    return cases


class ConfigCorpusShapeTests(unittest.TestCase):
    def test_published_config_schema_equals_generated_schema(self):
        published = json.loads(CONFIG_SCHEMA_PATH.read_text())
        self.assertEqual(published, project_json_schema())

    def test_config_asymmetry_registry_is_complete_and_published(self):
        declared = {item.id: item.description for item in CONFIG_SEMANTIC_ASYMMETRIES}
        covered = set()
        errors = []
        for label, _, schema_ok, python_ok, asymmetry_id in config_agreement_corpus():
            is_deliberate_asymmetry = (schema_ok, python_ok) == (True, False)
            if is_deliberate_asymmetry and asymmetry_id is None:
                errors.append(f"{label}: missing asymmetry id")
            if not is_deliberate_asymmetry and asymmetry_id is not None:
                errors.append(f"{label}: unexpected asymmetry id {asymmetry_id!r}")
            if asymmetry_id is not None:
                covered.add(asymmetry_id)
        self.assertEqual([], errors)
        self.assertEqual(set(declared), covered)

        comment = json.loads(CONFIG_SCHEMA_PATH.read_text()).get("$comment", "")
        for asymmetry_id, description in declared.items():
            with self.subTest(asymmetry_id=asymmetry_id):
                self.assertIn(f"{asymmetry_id}: {description}", comment)

    def test_config_corpus_covers_each_structural_group(self):
        counts = {}
        for row in config_agreement_corpus():
            group = _config_group_of(row)
            counts[group] = counts.get(group, 0) + 1
        self.assertEqual(
            {
                "valid": 1,
                "null_projection": 1,
                "required_valid": 1,
                "required_empty": 1,
                "structural": 38,
                "asymmetric": 9,
            },
            counts,
        )


class ConfigSchemaPythonDifferentialTests(unittest.TestCase):
    def test_every_config_corpus_case_matches_its_declared_outcome(self):
        validator = Draft202012Validator(json.loads(CONFIG_SCHEMA_PATH.read_text()))
        mismatches = []
        for label, config, schema_ok, python_ok, _ in config_agreement_corpus():
            got_schema = validator.is_valid(config)
            try:
                validate_project_config(config)
                got_python = True
            except ConfigValidationError:
                got_python = False
            expected = (schema_ok, python_ok)
            got = (got_schema, got_python)
            if got != expected:
                mismatches.append(
                    f"{label}: got schema={got_schema} python={got_python} "
                    f"expected schema={schema_ok} python={python_ok}"
                )
        self.assertEqual([], mismatches)

    def test_config_corpus_declares_both_agreement_and_deliberate_asymmetry(self):
        outcomes = {row[2:4] for row in config_agreement_corpus()}
        self.assertIn((True, True), outcomes)
        self.assertIn((False, False), outcomes)
        self.assertIn((True, False), outcomes)
        self.assertNotIn((False, True), outcomes)

    def test_config_validation_rejects_forbidden_paths_without_filesystem_io(self):
        config = _valid_config()
        config["repo_path"] = "/tmp/scratch/repo"
        with self.assertRaises(ConfigValidationError):
            validate_project_config(config)

    def test_config_validation_returns_typed_project_config(self):
        from pr_closure.model import ProjectConfig

        config = validate_project_config(_valid_config())
        self.assertIsInstance(config, ProjectConfig)
        self.assertEqual("publyapp", config.project)
        self.assertEqual("/var/tmp/durable/repo", config.repo_path)

    def test_config_forbidden_roots_match_store_forbidden_roots(self):
        from pr_closure.store import _FORBIDDEN_ROOT_SPECS

        self.assertEqual(CONFIG_FORBIDDEN_ROOT_SPECS, _FORBIDDEN_ROOT_SPECS)


def _config_group_of(row):
    label = row[0]
    if label == "valid config":
        return "valid"
    if label == "null tracking_projection":
        return "null_projection"
    if label == "valid ci_required_checks":
        return "required_valid"
    if label == "empty ci_required_checks legacy":
        return "required_empty"
    if row[4] is not None:
        return "asymmetric"
    return "structural"


def _disposition_of(row):
    record = row[1]
    return record["findings"][0]["disposition"]


def _group_of(label):
    if label == "valid record":
        return "valid"
    if label == "large pr_number remains valid":
        return "boundary"
    if label.startswith("explicit null follow_up_issue"):
        return "null_follow_up"
    if label in (
        "paired but empty proof arrays",
        "one empty proof array",
    ):
        return "paired_empty"
    if label.endswith("trailing newline"):
        return "trailing_newline"
    if label in (
        "duplicate finding IDs",
        "unknown family wizard-9000",
        "third-party lookalike claude-killer",
        "same family under different spellings",
        "schema_version 1.0",
        "pr_number 42.0",
        "follow_up_issue 900.0",
    ):
        return "asymmetric"
    if "blank " in label:
        return "whitespace"
    return "structural"


if __name__ == "__main__":
    unittest.main()
