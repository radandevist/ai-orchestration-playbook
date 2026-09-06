"""The normalized project route table is the only dispatch authority."""

from __future__ import annotations

from pr_closure.model import ModelRoute, ProjectConfig, ReviewPolicyMode


def select_route(routes, implementer_model: str) -> ModelRoute:
    matches = tuple(
        route for route in routes if route.implementer_model == implementer_model
    )
    if len(matches) != 1:
        raise ValueError("implementer model does not match exactly one normalized route")
    return matches[0]


def select_model_route(config: ProjectConfig, implementer_model: str) -> ModelRoute:
    if config.review_policy.mode is None:
        raise ValueError("disabled policy has no dispatch route")
    if config.review_policy.mode not in (
        ReviewPolicyMode.STAGED,
        ReviewPolicyMode.ENFORCED,
    ):
        raise ValueError("unsupported review policy mode")
    return select_route(config.model_routes, implementer_model)
