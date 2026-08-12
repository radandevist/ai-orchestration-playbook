from __future__ import annotations

import re
from typing import List, Optional, Tuple

from pr_closure.contract import ReviewValidationError

FAMILY_KEYWORDS = {
    "deepseek": frozenset({"deepseek"}),
    "openai": frozenset({"openai", "gpt", "chatgpt", "o1", "o3", "o4"}),
    "anthropic": frozenset({"anthropic", "claude"}),
    "google": frozenset({"google", "googleai", "gemini", "bard"}),
    "xai": frozenset({"xai", "grok"}),
    "zhipu": frozenset({"zhipu", "glm", "zai", "zhipuai"}),
    "alibaba": frozenset({"alibaba", "aliyun", "tongyi", "qwen"}),
    "moonshot": frozenset({"moonshot", "kimi"}),
    "minimax": frozenset({"minimax", "abab"}),
    "xiaomi": frozenset({"xiaomi", "mimo"}),
}

FAMILY_DESIGNATORS = frozenset({
    "mini", "pro", "max", "plus", "ultra", "nano", "alpha", "beta",
    "preview", "flash", "turbo", "lite", "vision", "chat", "instruct",
    "coder", "codex", "reasoning", "thinking", "high", "medium", "low",
    "large", "small", "opus", "sonnet", "haiku", "next", "exp",
})

_VERSION_RE = re.compile(r"^\.?\d[\d.]*$")
_V_PREFIX_RE = re.compile(r"^v?\d+(\.\d+)*$")
_RELEASE_RE = re.compile(r"^r\d+$")
_SIZE_RE = re.compile(r"^\d+(\.\d+)?[bm]$")
_ALNUM_VERSION_RE = re.compile(r"^[a-z]?\d+(\.\d+)*[a-z]?$")

_VERSION_MATCHERS = (
    _VERSION_RE,
    _V_PREFIX_RE,
    _RELEASE_RE,
    _SIZE_RE,
    _ALNUM_VERSION_RE,
)


def _is_version(token: str) -> bool:
    return any(regex.fullmatch(token) is not None for regex in _VERSION_MATCHERS)


def _is_suffix(suffix: str) -> bool:
    return suffix in FAMILY_DESIGNATORS or _is_version(suffix)


def _model_tokens(model_segment: str) -> List[str]:
    return re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", model_segment)


def family_of_token(token: str) -> Optional[str]:
    for family, keywords in FAMILY_KEYWORDS.items():
        if token in keywords:
            return family
        for keyword in keywords:
            if token.startswith(keyword) and _is_suffix(token[len(keyword):]):
                return family
    return None


def classify(token: str):
    """Classify a model-segment token as (kind, payload)."""
    family = family_of_token(token)
    if family is not None:
        return "family", family
    if token in FAMILY_DESIGNATORS:
        return "designator", token
    if _is_version(token):
        return "version", token
    return "codename", token


def _single_namespace_family(namespace_segment: str) -> Optional[str]:
    joined = "".join(char for char in namespace_segment if char.isalnum())
    if (family := family_of_token(joined)) is not None:
        return family
    for token in _model_tokens(namespace_segment):
        if (family := family_of_token(token)) is not None:
            return family
    return None


def _namespace_family(namespace_segments: List[str], original: str) -> Optional[str]:
    families = set()
    for segment in namespace_segments:
        family = _single_namespace_family(segment)
        if family is not None:
            families.add(family)
    if len(families) > 1:
        raise ReviewValidationError(f"ambiguous model family: {original!r}")
    return next(iter(families)) if families else None


def resolve_family(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewValidationError("model family must be a non-empty string")
    original = value
    normalized = value.strip().casefold()
    segments = [segment for segment in normalized.split("/") if segment]
    if not segments:
        raise ReviewValidationError(f"unknown model family: {original!r}")
    namespace_segments, model_segment = segments[:-1], segments[-1]
    namespace_family = _namespace_family(namespace_segments, original)

    tokens = _model_tokens(model_segment)
    head_family = family_of_token(tokens[0]) if tokens else None
    if head_family is None:
        if namespace_family is not None:
            return namespace_family
        raise ReviewValidationError(f"unknown model family: {original!r}")
    if namespace_family is not None and namespace_family != head_family:
        raise ReviewValidationError(f"ambiguous model family: {original!r}")
    family = head_family

    release_seen = False
    for token in tokens[1:]:
        kind, payload = classify(token)
        if kind == "family":
            if payload != family:
                raise ReviewValidationError(f"ambiguous model family: {original!r}")
        elif kind == "version":
            release_seen = True
        elif kind == "designator":
            pass
        elif kind == "codename":
            if not release_seen:
                raise ReviewValidationError(f"unknown model family: {original!r}")
    return family