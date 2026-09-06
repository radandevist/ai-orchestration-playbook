from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import FrozenSet, Mapping, Tuple


class RegistryValidationError(ValueError):
    """Raised when a model or launcher identity is not in its pinned registry."""


_MODEL_REGISTRY_V1 = {
    "gpt-5.6-luna": "openai",
    "gpt-5.6-sol": "openai",
    "gpt-5.6-terra": "openai",
    "gpt-5.3-codex-spark": "openai",
    "deepseek-v4-flash": "deepseek",
    "deepseek-v4-pro": "deepseek",
    "glm-5.2": "zhipu",
    "qwen3.7-max": "alibaba",
    "qwen3.7-plus": "alibaba",
    "kimi-k2.6": "moonshot",
    "kimi-k2.7-code": "moonshot",
    "kimi-k3": "moonshot",
    "minimax-m3": "minimax",
    "mimo-v2.5": "xiaomi",
    "mimo-v2.5-pro": "xiaomi",
    "claude-opus-5": "anthropic",
    "claude-sonnet-5": "anthropic",
}

MODEL_REGISTRIES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "models-v1": MappingProxyType(_MODEL_REGISTRY_V1),
})

MODEL_ALIASES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "models-v1": MappingProxyType({}),
})

MODEL_REGISTRY_GOLDEN_SHA256: Mapping[str, str] = MappingProxyType({
    "models-v1": "8b5e8673ab6d56bcc1d7f435b79ba8503f08057ec820901919dcc5052a8ec0c2",
})

LauncherIdentity = Tuple[str, str, str]

_LAUNCHER_REGISTRY_V1: FrozenSet[LauncherIdentity] = frozenset({
    ("deepseek-v4-flash", "opencode", "cline-pass/cline-pass/deepseek-v4-flash"),
    ("gpt-5.6-luna", "codex", "gpt-5.6-luna"),
    ("gpt-5.6-sol", "codex", "gpt-5.6-sol"),
})

LAUNCHER_REGISTRIES: Mapping[str, FrozenSet[LauncherIdentity]] = MappingProxyType({
    "launchers-v1": _LAUNCHER_REGISTRY_V1,
})

LAUNCHER_REGISTRY_GOLDEN_SHA256: Mapping[str, str] = MappingProxyType({
    "launchers-v1": "b7a4dd007921ffe1bf57e7c01a02bec95b9a32013d959500e411b7d9c88416c6",
})


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def verify_released_registries() -> None:
    for version, registry in MODEL_REGISTRIES.items():
        expected = MODEL_REGISTRY_GOLDEN_SHA256.get(version)
        if expected is None or _sha256(dict(registry)) != expected:
            raise RegistryValidationError(
                "model registry {0} does not match its released digest".format(version)
            )

    for version, registry in LAUNCHER_REGISTRIES.items():
        expected = LAUNCHER_REGISTRY_GOLDEN_SHA256.get(version)
        serialized = [list(endpoint) for endpoint in sorted(registry)]
        if expected is None or _sha256(serialized) != expected:
            raise RegistryValidationError(
                "launcher registry {0} does not match its released digest".format(version)
            )


def require_model(registry_version: str, model_id: str) -> str:
    registry = MODEL_REGISTRIES.get(registry_version)
    if registry is None:
        raise RegistryValidationError(
            "unknown model registry version: {0}".format(registry_version)
        )
    family = registry.get(model_id)
    if family is None:
        raise RegistryValidationError(
            "model_id is not an exact key in {0}: {1}".format(
                registry_version,
                repr(model_id),
            )
        )
    return family


def require_launcher(
    model_registry_version: str,
    launcher_registry_version: str,
    model_id: str,
    runner: str,
    invocation_model: str,
) -> None:
    require_model(model_registry_version, model_id)
    registry = LAUNCHER_REGISTRIES.get(launcher_registry_version)
    if registry is None:
        raise RegistryValidationError(
            "unknown launcher registry version: {0}".format(launcher_registry_version)
        )
    endpoint = (model_id, runner, invocation_model)
    if endpoint not in registry:
        raise RegistryValidationError(
            "launcher endpoint is not an exact member of {0}: {1}".format(
                launcher_registry_version,
                repr(endpoint),
            )
        )
