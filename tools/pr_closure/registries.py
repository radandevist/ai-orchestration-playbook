from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import FrozenSet, Mapping, Tuple


class RegistryValidationError(ValueError):
    """Raised when a model or launcher identity is not in its pinned registry."""


@dataclass(frozen=True)
class RepositoryPolicyFloor:
    """Immutable minimum review policy for one exact repository identity."""

    registry_version: str
    repository: str
    policy_id: str
    policy_digest: str
    definition: Mapping


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

def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


_PUBLYAPP_POLICY_DEFINITION = {
    "owner_authorization": "Radan; owner instruction 2026-09-05",
    "forbidden_reviewer_families": ["anthropic"],
    "same_family_exceptions": [
        {
            "id": "publyapp-gpt-implementation-sol-review-v1",
            "registry_version": "models-v1",
            "implementer_family": "openai",
            "reviewer_model": "gpt-5.6-sol",
            "required_for_authorized_family": True,
            "owner_authorization": "Radan; owner instruction 2026-09-05",
            "rationale": "GPT implementation is reviewed by gpt-5.6-sol; Claude is forbidden.",
        }
    ],
    "model_routes": [
        {
            "id": "publyapp-luna-to-sol-v1",
            "registry_version": "models-v1",
            "launcher_registry_version": "launchers-v1",
            "implementer_model": "gpt-5.6-luna",
            "implementer_runner": "codex",
            "implementer_invocation_model": "gpt-5.6-luna",
            "reviewer_model": "gpt-5.6-sol",
            "reviewer_runner": "codex",
            "reviewer_invocation_model": "gpt-5.6-sol",
            "same_family_policy_id": "publyapp-gpt-implementation-sol-review-v1",
        },
        {
            "id": "publyapp-deepseek-to-sol-v1",
            "registry_version": "models-v1",
            "launcher_registry_version": "launchers-v1",
            "implementer_model": "deepseek-v4-flash",
            "implementer_runner": "opencode",
            "implementer_invocation_model": "cline-pass/cline-pass/deepseek-v4-flash",
            "reviewer_model": "gpt-5.6-sol",
            "reviewer_runner": "codex",
            "reviewer_invocation_model": "gpt-5.6-sol",
            "same_family_policy_id": None,
        },
    ],
}

_PUBLYAPP_POLICY_DIGEST = "f177780e94a565bb4dacea068faa3866986e186e297dfb2e5eedcf1181209e2f"
_PUBLYAPP_POLICY_ID = "review-policy-" + _PUBLYAPP_POLICY_DIGEST
_PUBLYAPP_POLICY_FLOOR = RepositoryPolicyFloor(
    registry_version="project-policies-v1",
    repository="PublyApp/publyapp",
    policy_id=_PUBLYAPP_POLICY_ID,
    policy_digest=_PUBLYAPP_POLICY_DIGEST,
    definition=_freeze(_PUBLYAPP_POLICY_DEFINITION),
)

REPOSITORY_POLICY_REGISTRIES: Mapping[str, Mapping[str, RepositoryPolicyFloor]] = MappingProxyType({
    "project-policies-v1": MappingProxyType({
        "PublyApp/publyapp": _PUBLYAPP_POLICY_FLOOR,
    }),
})

REPOSITORY_POLICY_REGISTRY_GOLDEN_SHA256: Mapping[str, str] = MappingProxyType({
    "project-policies-v1": "44deb1c04121c5cb5fdebd02b4c36263f259342acaa910cf989e016f3023fb45",
})


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def canonical_policy_definition(policy, routes) -> dict:
    """Return the mode-independent canonical identity of normalized policy data."""
    return {
        "owner_authorization": policy.owner_authorization,
        "forbidden_reviewer_families": list(policy.forbidden_reviewer_families),
        "same_family_exceptions": [
            {name: getattr(exception, name) for name in exception.__dataclass_fields__}
            for exception in policy.same_family_exceptions
        ],
        "model_routes": [
            {name: getattr(route, name) for name in route.__dataclass_fields__}
            for route in routes
        ],
    }


def policy_floor_for(repository: str) -> RepositoryPolicyFloor | None:
    """Return the exact released floor, or ``None`` for an unregistered repository."""
    verify_released_registries()
    for registry in REPOSITORY_POLICY_REGISTRIES.values():
        floor = registry.get(repository)
        if floor is not None:
            return floor
    return None


def require_policy_floor(repository: str, policy, routes) -> None:
    """Enforce a registered repository's immutable policy minimum."""
    floor = policy_floor_for(repository)
    if floor is None:
        return
    if policy.mode is None:
        raise RegistryValidationError(
            "registered repository requires its immutable review policy floor"
        )
    definition = canonical_policy_definition(policy, routes)
    digest = _sha256(definition)
    if digest != floor.policy_digest or definition != _thaw(floor.definition):
        raise RegistryValidationError(
            "configuration does not satisfy the immutable repository policy floor"
        )
    if "review-policy-" + digest != floor.policy_id:
        raise RegistryValidationError(
            "released repository policy floor identity is internally inconsistent"
        )


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

    for version, registry in REPOSITORY_POLICY_REGISTRIES.items():
        serialized = {
            repository: {
                "registry_version": floor.registry_version,
                "repository": floor.repository,
                "policy_id": floor.policy_id,
                "policy_digest": floor.policy_digest,
                "definition": _thaw(floor.definition),
            }
            for repository, floor in sorted(registry.items())
        }
        expected = REPOSITORY_POLICY_REGISTRY_GOLDEN_SHA256.get(version)
        if expected is None or _sha256(serialized) != expected:
            raise RegistryValidationError(
                "repository policy registry {0} does not match its released digest".format(version)
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
