import unittest

from pr_closure.registries import (
    LAUNCHER_REGISTRIES,
    LAUNCHER_REGISTRY_GOLDEN_SHA256,
    MODEL_ALIASES,
    MODEL_REGISTRIES,
    MODEL_REGISTRY_GOLDEN_SHA256,
    RegistryValidationError,
    require_launcher,
    require_model,
    verify_released_registries,
)


EXPECTED_MODELS_V1 = {
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

EXPECTED_LAUNCHERS_V1 = frozenset({
    ("deepseek-v4-flash", "opencode", "cline-pass/cline-pass/deepseek-v4-flash"),
    ("gpt-5.6-luna", "codex", "gpt-5.6-luna"),
    ("gpt-5.6-sol", "codex", "gpt-5.6-sol"),
})


class ReleasedRegistryTests(unittest.TestCase):
    def test_models_v1_is_the_complete_released_snapshot(self):
        self.assertEqual(EXPECTED_MODELS_V1, dict(MODEL_REGISTRIES["models-v1"]))
        self.assertEqual(
            "8b5e8673ab6d56bcc1d7f435b79ba8503f08057ec820901919dcc5052a8ec0c2",
            MODEL_REGISTRY_GOLDEN_SHA256["models-v1"],
        )

    def test_models_v1_aliases_are_exactly_empty(self):
        self.assertEqual({}, dict(MODEL_ALIASES["models-v1"]))

    def test_launchers_v1_is_the_complete_released_snapshot(self):
        self.assertEqual(EXPECTED_LAUNCHERS_V1, LAUNCHER_REGISTRIES["launchers-v1"])
        self.assertEqual(
            "b7a4dd007921ffe1bf57e7c01a02bec95b9a32013d959500e411b7d9c88416c6",
            LAUNCHER_REGISTRY_GOLDEN_SHA256["launchers-v1"],
        )

    def test_released_registry_digests_verify(self):
        verify_released_registries()

    def test_model_lookup_is_byte_exact_and_version_pinned(self):
        self.assertEqual("openai", require_model("models-v1", "gpt-5.6-luna"))
        for version, model_id in (
            ("models-v2", "gpt-5.6-luna"),
            ("models-v1", "gpt"),
            ("models-v1", "openai"),
            ("models-v1", "GPT-5.6-LUNA"),
            ("models-v1", " gpt-5.6-luna"),
            ("models-v1", "gpt-5.6-luna "),
            ("models-v1", "gpt／5.6-luna"),
        ):
            with self.subTest(version=version, model_id=model_id):
                with self.assertRaises(RegistryValidationError):
                    require_model(version, model_id)

    def test_launcher_lookup_requires_the_exact_endpoint(self):
        require_launcher(
            "models-v1",
            "launchers-v1",
            "gpt-5.6-luna",
            "codex",
            "gpt-5.6-luna",
        )
        for endpoint in (
            ("gpt-5.6-luna", "jcode", "gpt-5.6-luna"),
            ("gpt-5.6-luna", "codex", "luna"),
            ("deepseek-v4-flash", "opencode", "deepseek-v4-flash"),
            ("gpt-5.6-terra", "codex", "gpt-5.6-terra"),
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(RegistryValidationError):
                    require_launcher("models-v1", "launchers-v1", *endpoint)

    def test_launcher_version_has_no_latest_fallback(self):
        with self.assertRaises(RegistryValidationError):
            require_launcher(
                "models-v1",
                "launchers-v2",
                "gpt-5.6-luna",
                "codex",
                "gpt-5.6-luna",
            )


if __name__ == "__main__":
    unittest.main()
