"""Advisory inventory of legacy active-tip review artifacts."""

from __future__ import annotations

import json


def _participant_inventory(record, role):
    prefix = role + "_"
    return {
        "model_id": record.get(prefix + "model"),
        "family": record.get(prefix + "family"),
        "runner": record.get(prefix + "runner"),
        "invocation_model": record.get(prefix + "invocation_model"),
        "durable_path": record.get(prefix + "durable_path"),
        "sha256": record.get(prefix + "sha256"),
    }


def inventory_active_tip(store, commit):
    """Return every legacy artifact at ``commit`` without granting authority."""
    report = []
    for path in store.review_paths(commit):
        raw, raw_sha256 = store._read_bound_bytes(path, "legacy review record")
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("legacy review record is not valid UTF-8 JSON") from error
        if not isinstance(record, dict):
            raise ValueError("legacy review record must be a JSON object")
        report.append(
            {
                "review_id": path.stem,
                "schema_version": record.get("schema_version"),
                "raw_sha256": raw_sha256,
                "active_tip": record.get("reviewed_commit") == commit,
                "authority": False,
                "source_path": str(path),
                "provenance": {
                    "implementer": _participant_inventory(record, "implementer"),
                    "reviewer": _participant_inventory(record, "reviewer"),
                },
            }
        )
    return tuple(report)
