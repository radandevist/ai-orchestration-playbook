import copy
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from pr_closure.contract import COMMIT_ID_PATTERN
from pr_closure.model import (
    CiState,
    ClosureSnapshot,
    ClosureState,
    Contradiction,
    Evidence,
    Verdict,
)
from pr_closure.state import derive_state

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"
FIXTURE_DIRS = ("digital_prevention", "publyapp")

FIXTURE_VERSION = 1
REQUIRED_KEYS = (
    "fixture_version",
    "scenario",
    "expected_state",
    "expected_blocking_ids",
    "must_not_state",
    "snapshot",
)

SNAPSHOT_FIELDS = frozenset(ClosureSnapshot.__dataclass_fields__)
COMMIT_FIELDS = (
    "local_commit",
    "remote_commit",
    "ci_commit",
    "review_commit",
    "verification_commit",
    "durable_tip",
)
NULLABLE_BOOL_FIELDS = (
    "worktree_clean",
    "local_verification",
    "follow_ups_complete",
    "pr_is_draft",
)
BOOL_FIELDS = ("fixing_lane_active", "review_owned", "owner_decision_required")
INT_FIELDS = (
    "infra_retry_budget",
    "infra_retries_used",
    "distinct_repair_strategies",
    "executor_deaths",
    "stagnation_budget_minutes",
)
STR_FIELDS = (
    "head_branch",
    "base_branch",
    "checked_out_branch",
    "pr_state",
    "repeated_root_cause",
)
STRING_LIST_FIELDS = ("blocking_findings", "follow_up_findings")

# States whose derived reasons carry the snapshot blocking finding IDs
# verbatim. Other states surface evidence strings instead, so the fixture
# scenario documents why the named finding is not echoed in the reasons.
_REASON_CARRYING_STATES = frozenset({ClosureState.CHANGES_REQUIRED})

# Fixed synthetic clock for pure derivation; fixtures themselves are timeless.
NOW = datetime(2000, 1, 1, 12, 0, 0)

_COMMIT_ID_RE = re.compile(COMMIT_ID_PATTERN)


def load_fixtures():
    """Load and validate every fixture; raise on any contract violation.

    Rejects unknown/missing keys, unknown state/CI/verdict values, malformed
    commit shapes, duplicate scenario IDs, and non-array blocking IDs.
    """
    fixtures = []
    seen_scenarios = {}
    for dirname in FIXTURE_DIRS:
        fixture_dir = FIXTURES_ROOT / dirname
        if not fixture_dir.is_dir():
            raise FileNotFoundError(f"missing fixture directory: {fixture_dir}")
        for path in sorted(fixture_dir.glob("*.json")):
            raw = json.loads(path.read_text())
            _validate_fixture(raw, path)
            scenario = raw["scenario"]
            if scenario in seen_scenarios:
                raise ValueError(
                    f"duplicate scenario id {scenario!r} in {path} and {seen_scenarios[scenario]}"
                )
            seen_scenarios[scenario] = path
            fixtures.append((path, raw, _build_snapshot(raw["snapshot"], path)))
    return fixtures


def _validate_fixture(raw, path):
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: fixture must be a JSON object")

    unknown = set(raw) - set(REQUIRED_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown fixture keys {sorted(unknown)}")
    missing = set(REQUIRED_KEYS) - set(raw)
    if missing:
        raise ValueError(f"{path}: missing fixture keys {sorted(missing)}")

    if raw["fixture_version"] != FIXTURE_VERSION:
        raise ValueError(f"{path}: fixture_version must be {FIXTURE_VERSION}")
    if not isinstance(raw["scenario"], str) or not raw["scenario"].strip():
        raise ValueError(f"{path}: scenario must be a non-empty string")
    _validate_state(raw["expected_state"], path, "expected_state")
    if raw["must_not_state"] is not None:
        _validate_state(raw["must_not_state"], path, "must_not_state")
    _validate_blocking_ids(raw["expected_blocking_ids"], path)
    if not isinstance(raw["snapshot"], dict):
        raise ValueError(f"{path}: snapshot must be a JSON object")
    _validate_snapshot(raw["snapshot"], path)


def _validate_state(value, path, key):
    if value not in ClosureState.__members__:
        raise ValueError(f"{path}: unknown {key} state {value!r}")


def _validate_blocking_ids(value, path):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path}: expected_blocking_ids must be an array of strings")


def _validate_snapshot(snapshot, path):
    unknown = set(snapshot) - SNAPSHOT_FIELDS
    if unknown:
        raise ValueError(f"{path}: unknown snapshot keys {sorted(unknown)}")

    for key in ("evidence_available", "contradictions"):
        value = snapshot.get(key)
        if value is not None:
            _validate_enum_list(value, Evidence if key == "evidence_available" else Contradiction, path, key)

    if "ci_state" in snapshot and snapshot["ci_state"] not in CiState.__members__:
        raise ValueError(f"{path}: unknown ci_state {snapshot['ci_state']!r}")
    if "review_verdict" in snapshot and snapshot["review_verdict"] is not None \
            and snapshot["review_verdict"] not in Verdict.__members__:
        raise ValueError(f"{path}: unknown review_verdict {snapshot['review_verdict']!r}")

    for key in COMMIT_FIELDS:
        value = snapshot.get(key)
        if value is not None and _COMMIT_ID_RE.fullmatch(value) is None:
            raise ValueError(f"{path}: malformed {key} commit shape {value!r}")

    for key in NULLABLE_BOOL_FIELDS:
        value = snapshot.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{path}: {key} must be a bool or null")
    for key in BOOL_FIELDS:
        if key in snapshot and not isinstance(snapshot[key], bool):
            raise ValueError(f"{path}: {key} must be a bool")
    for key in INT_FIELDS:
        if key in snapshot and (not isinstance(snapshot[key], int) or isinstance(snapshot[key], bool)):
            raise ValueError(f"{path}: {key} must be an integer")
    for key in STR_FIELDS:
        value = snapshot.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{path}: {key} must be a string or null")
    for key in STRING_LIST_FIELDS:
        value = snapshot.get(key)
        if value is not None:
            _validate_enum_list(value, None, path, key)
    if "last_progress_at" in snapshot and snapshot["last_progress_at"] is not None:
        try:
            datetime.fromisoformat(snapshot["last_progress_at"])
        except ValueError as error:
            raise ValueError(f"{path}: last_progress_at must be ISO-8601 or null") from error


def _validate_enum_list(value, enum, path, key):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path}: {key} must be an array of strings")
    if enum is not None:
        valid = {member.value for member in enum}
        for item in value:
            if item not in valid:
                raise ValueError(f"{path}: unknown {key} value {item!r}")


def _build_snapshot(snapshot, path):
    kwargs = dict(snapshot)
    for key in ("evidence_available", "contradictions"):
        if key in kwargs and kwargs[key] is not None:
            enum = Evidence if key == "evidence_available" else Contradiction
            kwargs[key] = frozenset(enum(item) for item in kwargs[key])
    if "ci_state" in kwargs:
        kwargs["ci_state"] = CiState(kwargs["ci_state"])
    if kwargs.get("review_verdict") is not None:
        kwargs["review_verdict"] = Verdict(kwargs["review_verdict"])
    for key in STRING_LIST_FIELDS:
        if key in kwargs and kwargs[key] is not None:
            kwargs[key] = tuple(kwargs[key])
    if kwargs.get("last_progress_at") is not None:
        kwargs["last_progress_at"] = datetime.fromisoformat(kwargs["last_progress_at"])
    return ClosureSnapshot(**kwargs)


class FixtureValidationTests(unittest.TestCase):
    """The fixture contract is enforced before any derivation runs."""

    def test_all_fixtures_validate_and_scenarios_are_unique(self):
        fixtures = load_fixtures()
        self.assertGreaterEqual(len(fixtures), 9)
        scenarios = [fixture[1]["scenario"] for fixture in fixtures]
        self.assertEqual(len(scenarios), len(set(scenarios)))


class FixtureContractRejectionTests(unittest.TestCase):
    """Negative coverage: the helper must reject every malformed shape."""

    @classmethod
    def setUpClass(cls):
        cls.good = load_fixtures()[0][1]

    def _expect_reject(self, mutate):
        raw = copy.deepcopy(self.good)
        mutate(raw)
        with self.assertRaises(ValueError):
            _validate_fixture(raw, Path("fixture.json"))

    def test_unknown_top_level_key_rejected(self):
        self._expect_reject(lambda raw: raw.__setitem__("bogus", 1))

    def test_missing_top_level_key_rejected(self):
        self._expect_reject(lambda raw: raw.pop("snapshot"))

    def test_unknown_expected_state_rejected(self):
        self._expect_reject(lambda raw: raw.__setitem__("expected_state", "NOT_A_STATE"))

    def test_unknown_must_not_state_rejected(self):
        self._expect_reject(lambda raw: raw.__setitem__("must_not_state", "NOT_A_STATE"))

    def test_non_array_blocking_ids_rejected(self):
        self._expect_reject(lambda raw: raw.__setitem__("expected_blocking_ids", "DP-X"))

    def test_unknown_snapshot_key_rejected(self):
        self._expect_reject(lambda raw: raw["snapshot"].__setitem__("bogus", 1))

    def test_unknown_ci_state_rejected(self):
        self._expect_reject(lambda raw: raw["snapshot"].__setitem__("ci_state", "MAYBE"))

    def test_unknown_verdict_rejected(self):
        self._expect_reject(lambda raw: raw["snapshot"].__setitem__("review_verdict", "MAYBE"))

    def test_malformed_commit_shape_rejected(self):
        self._expect_reject(lambda raw: raw["snapshot"].__setitem__("local_commit", "a" * 39))

    def test_duplicate_scenario_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for dirname in FIXTURE_DIRS:
                (root / dirname).mkdir()
            (root / FIXTURE_DIRS[0] / "one.json").write_text(json.dumps(self.good))
            (root / FIXTURE_DIRS[1] / "two.json").write_text(json.dumps(self.good))
            with mock.patch.object(sys.modules[__name__], "FIXTURES_ROOT", root):
                with self.assertRaises(ValueError):
                    load_fixtures()


class FixtureDerivationTests(unittest.TestCase):
    """Every fixture builds a real ClosureSnapshot and derives through the
    production state machine; the expected answer comes from the fixture."""

    def test_derived_state_matches_every_fixture(self):
        fixtures = load_fixtures()
        for path, fixture, snapshot in fixtures:
            with self.subTest(scenario=fixture["scenario"], path=str(path)):
                decision = derive_state(snapshot, NOW)
                self.assertEqual(fixture["expected_state"], decision.state.value, str(path))
                self.assertNotEqual(fixture["must_not_state"], decision.state.value, str(path))
                self.assertEqual(
                    sorted(fixture["expected_blocking_ids"]),
                    sorted(snapshot.blocking_findings),
                    str(path),
                )
                if decision.state in _REASON_CARRYING_STATES:
                    for finding_id in fixture["expected_blocking_ids"]:
                        self.assertIn(finding_id, decision.reasons, str(path))


if __name__ == "__main__":
    unittest.main()
