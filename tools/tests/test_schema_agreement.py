import importlib.metadata
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from pr_closure.review import ReviewValidationError, validate_review

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "review-record-v1.json"
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
    """(label, record, expected_schema_valid, expected_python_valid) tuples.

    Agreement and deliberate asymmetry are both first-class members; no assertion
    privileges either outcome.
    """

    def add(label, mutate, schema_ok, python_ok):
        record = _valid_record()
        mutate(record)
        cases.append((label, record, schema_ok, python_ok))

    cases = []

    add("valid record", lambda r: None, True, True)

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
    )
    add(
        "unknown family wizard-9000",
        lambda r: r.update({"implementer_family": "wizard-9000"}),
        True,
        False,
    )
    add(
        "third-party lookalike claude-killer",
        lambda r: r.update({"implementer_family": "claude-killer"}),
        True,
        False,
    )
    add(
        "same family under different spellings",
        lambda r: r.update({"reviewer_family": "deepseek-v4-flash"}),
        True,
        False,
    )
    add("schema_version 1.0", lambda r: r.update({"schema_version": 1.0}), True, False)

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
        for label, record, schema_ok, python_ok in agreement_corpus():
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
        outcomes = {row[2:] for row in agreement_corpus()}
        self.assertTrue((True, True) in outcomes)
        self.assertTrue((False, False) in outcomes)
        self.assertTrue((True, False) in outcomes)
        self.assertNotIn((False, True), outcomes)

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
        for label, _, schema_ok, python_ok in agreement_corpus():
            group = _group_of(label)
            counts[group] = counts.get(group, 0) + 1
        self.assertEqual(
            {
                "valid": 1,
                "structural": 13,
                "whitespace": 12,
                "null_follow_up": 3,
                "paired_empty": 2,
                "trailing_newline": 3,
                "asymmetric": 5,
            },
            counts,
        )


def _disposition_of(row):
    record = row[1]
    return record["findings"][0]["disposition"]


def _group_of(label):
    if label == "valid record":
        return "valid"
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
    ):
        return "asymmetric"
    if "blank " in label:
        return "whitespace"
    return "structural"


if __name__ == "__main__":
    unittest.main()