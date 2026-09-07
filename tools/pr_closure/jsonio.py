"""Bounded, duplicate-rejecting JSON decoding for authority boundaries."""

from __future__ import annotations

import json

MAX_JSON_BYTES = 2 * 1024 * 1024


class StrictJsonError(ValueError):
    """The input is not a bounded, unique-key JSON document."""


def loads(raw: str | bytes, label: str = "JSON", *, max_bytes: int = MAX_JSON_BYTES):
    if isinstance(raw, bytes):
        size = len(raw)
        text = raw.decode("utf-8")
    elif isinstance(raw, str):
        text = raw
        size = len(raw.encode("utf-8"))
    else:
        raise StrictJsonError(f"{label} must be text or bytes")
    if size > max_bytes:
        raise StrictJsonError(f"{label} exceeds the {max_bytes}-byte limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise StrictJsonError(f"{label} contains duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrictJsonError(f"{label} is malformed JSON: {error}") from error
