"""Offline tests for independent role-based LLM configuration."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import src.config.settings as settings_module
from src.config.llm_deployments import normalize_chat_endpoint
from src.config.settings import Settings, get_settings
# Load the context package before the provider module to match application startup order.
import src.core.context  # noqa: F401
from src.core.llm.client import LLMClient


BASE_ENV = {
    "SOORIN_LLM_ENABLED": "true",
    "SOORIN_LLM_PROVIDER": "arvan",
    "SOORIN_LLM_AUTH_SCHEME": "apikey",
    "SOORIN_LLM_CHAT_PATH": "/chat/completions",
    "SOORIN_LLM_CONNECT_TIMEOUT_SECONDS": "8",
    "SOORIN_ROUTER_BASE_URL": "https://router.example.invalid/v1",
    "SOORIN_ROUTER_MODEL": "CHANGE_ME_MODEL",
    "SOORIN_ROUTER_API_KEY": "router-secret",
    "SOORIN_ROUTER_TIMEOUT_SECONDS": "30",
    "SOORIN_ROUTER_MAX_TOKENS": "924",
    "SOORIN_ROUTER_RETRY_MAX_TOKENS": "1284",
    "SOORIN_ROUTER_TEMPERATURE": "0.0",
    "SOORIN_ROUTER_TOP_P": "0.1",
    "SOORIN_ROUTER_SUPPORTS_TEMPERATURE": "true",
    "SOORIN_ROUTER_SUPPORTS_TOP_P": "true",
    "SOORIN_PLANNER_BASE_URL": "https://planner.example.invalid/v1",
    "SOORIN_PLANNER_MODEL": "CHANGE_ME_MODEL",
    "SOORIN_PLANNER_API_KEY": "planner-secret",
    "SOORIN_PLANNER_TIMEOUT_SECONDS": "120",
    "SOORIN_PLANNER_MAX_TOKENS": "2048",
    "SOORIN_PLANNER_RETRY_MAX_TOKENS": "3072",
    "SOORIN_PLANNER_TEMPERATURE": "0.1",
    "SOORIN_PLANNER_TOP_P": "0.8",
    "SOORIN_PLANNER_SUPPORTS_TEMPERATURE": "true",
    "SOORIN_PLANNER_SUPPORTS_TOP_P": "true",
    "SOORIN_SYNTHESIZER_BASE_URL": "https://synth.example.invalid/v1",
    "SOORIN_SYNTHESIZER_MODEL": "CHANGE_ME_MODEL",
    "SOORIN_SYNTHESIZER_API_KEY": "synth-secret",
    "SOORIN_SYNTHESIZER_TIMEOUT_SECONDS": "360",
    "SOORIN_SYNTHESIZER_MAX_TOKENS": "4096",
    "SOORIN_SYNTHESIZER_RETRY_MAX_TOKENS": "4096",
    "SOORIN_SYNTHESIZER_TEMPERATURE": "0.3",
    "SOORIN_SYNTHESIZER_TOP_P": "0.9",
    "SOORIN_SYNTHESIZER_SUPPORTS_TEMPERATURE": "false",
    "SOORIN_SYNTHESIZER_SUPPORTS_TOP_P": "false",
    "SOORIN_PLANNER_ENABLED": "true",
    "SOORIN_PLANNER_REPAIR_ENABLED": "true",
    "SOORIN_LLM_MAX_TRANSIENT_RETRIES": "0",
}


class FakeResponse:
    status_code = 200

    def __init__(self, model: str = "CHANGE_ME_MODEL") -> None:
        self.model = model

    def json(self) -> dict[str, object]:
        return {
            "model": self.model,
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }


def isolated_settings(**overrides: str) -> Settings:
    environment = dict(BASE_ENV)
    environment.update(overrides)
    missing_env = Path("/tmp/soorin-role-config-tests-missing.env")
    with patch.dict(os.environ, environment, clear=True), patch.object(settings_module, "ENV_PATH", missing_env):
        get_settings.cache_clear()
        try:
            return get_settings()
        finally:
            get_settings.cache_clear()


class RoleConfigurationTests(unittest.TestCase):
    def test_roles_resolve_independently_to_safe_placeholders(self) -> None:
        settings = isolated_settings()

        self.assertEqual(settings.role("router").model, "CHANGE_ME_MODEL")
        self.assertEqual(settings.role("planner").model, "CHANGE_ME_MODEL")
        self.assertEqual(settings.role("synthesizer").model, "CHANGE_ME_MODEL")
        self.assertEqual(settings.deployment_for_purpose("intent_router").name, "router")
        self.assertEqual(settings.deployment_for_purpose("planner").name, "planner")
        self.assertEqual(settings.deployment_for_purpose("chat").name, "synthesizer")

    def test_future_mixed_configuration_is_independent_per_role(self) -> None:
        settings = isolated_settings(
            SOORIN_ROUTER_BASE_URL="https://r.example.invalid/v1",
            SOORIN_ROUTER_MODEL="router-model",
            SOORIN_ROUTER_API_KEY="router-key",
            SOORIN_PLANNER_BASE_URL="https://p.example.invalid/v1",
            SOORIN_PLANNER_MODEL="planner-model",
            SOORIN_PLANNER_API_KEY="planner-key",
            SOORIN_SYNTHESIZER_BASE_URL="https://s.example.invalid/v1",
            SOORIN_SYNTHESIZER_MODEL="synth-model",
            SOORIN_SYNTHESIZER_API_KEY="synth-key",
        )

        self.assertEqual(settings.role("router").endpoint, "https://r.example.invalid/v1/chat/completions")
        self.assertEqual(settings.role("planner").model, "planner-model")
        self.assertEqual(settings.role("synthesizer").api_key, "synth-key")

    def test_endpoint_normalization_is_shared(self) -> None:
        self.assertEqual(
            normalize_chat_endpoint("https://role.example.invalid/v1/", "chat/completions"),
            "https://role.example.invalid/v1/chat/completions",
        )

    def test_missing_enabled_role_endpoint_fails_validation(self) -> None:
        settings = isolated_settings(SOORIN_PLANNER_BASE_URL="")
        with self.assertRaisesRegex(ValueError, "planner"):
            settings.validate_selected_llm_deployments()

    def test_role_specific_transport_preserves_payload_and_auth_semantics(self) -> None:
        settings = isolated_settings()
        client = LLMClient(settings)
        messages = [{"role": "user", "content": "hello"}]

        with patch("src.core.llm.providers.arvan.requests.post", side_effect=[FakeResponse()] * 3) as post:
            client.chat(messages, purpose="intent_router", transient_retries=0)
            client.chat(messages, purpose="planner", transient_retries=0)
            client.chat(messages, purpose="chat", transient_retries=0)

        self.assertEqual(post.call_count, 3)
        router_call, planner_call, synth_call = post.call_args_list
        self.assertEqual(router_call.args[0], "https://router.example.invalid/v1/chat/completions")
        self.assertEqual(planner_call.args[0], "https://planner.example.invalid/v1/chat/completions")
        self.assertEqual(synth_call.args[0], "https://synth.example.invalid/v1/chat/completions")
        self.assertEqual(router_call.kwargs["headers"]["Authorization"], "apikey router-secret")
        self.assertEqual(planner_call.kwargs["headers"]["Authorization"], "apikey planner-secret")
        self.assertEqual(synth_call.kwargs["headers"]["Authorization"], "apikey synth-secret")
        self.assertEqual(router_call.kwargs["json"]["max_tokens"], 924)
        self.assertEqual(planner_call.kwargs["json"]["max_tokens"], 2048)
        self.assertEqual(synth_call.kwargs["json"]["max_tokens"], 4096)
        self.assertEqual(router_call.kwargs["json"]["temperature"], 0.0)
        self.assertEqual(planner_call.kwargs["json"]["top_p"], 0.8)
        self.assertNotIn("temperature", synth_call.kwargs["json"])
        self.assertNotIn("top_p", synth_call.kwargs["json"])

    def test_keys_are_not_logged(self) -> None:
        settings = isolated_settings()
        with self.assertLogs("src.core.llm", level=logging.INFO) as captured:
            with patch("src.core.llm.providers.arvan.requests.post", return_value=FakeResponse()):
                LLMClient(settings).chat([{"role": "user", "content": "hello"}], purpose="chat", transient_retries=0)
        serialized = "\n".join(captured.output)
        self.assertNotIn("router-secret", serialized)
        self.assertNotIn("planner-secret", serialized)
        self.assertNotIn("synth-secret", serialized)

    def test_env_example_has_role_variables_and_no_obsolete_model_names(self) -> None:
        text = (settings_module.APP_DIR.parent / ".env.example").read_text(encoding="utf-8")
        self.assertIn("SOORIN_ROUTER_MODEL=CHANGE_ME_MODEL", text)
        self.assertIn("SOORIN_PLANNER_MODEL=CHANGE_ME_MODEL", text)
        self.assertIn("SOORIN_SYNTHESIZER_MODEL=CHANGE_ME_MODEL", text)
        self.assertNotIn("SOORIN_LLM_KIMI_", text)
        self.assertNotIn("SOORIN_LLM_GLM_", text)
        self.assertNotIn("SOORIN_LLM_GPT55_", text)
        self.assertNotIn("SOORIN_INTENT_ROUTER_DEPLOYMENT", text)
        self.assertNotIn("SOORIN_CHAT_DEPLOYMENT", text)
        self.assertNotIn("SOORIN_PLANNER_DEPLOYMENT", text)
        self.assertEqual(text.count("https://"), 0)
        self.assertEqual(len([line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#") and "=" in line]), len({line.split("=", 1)[0].strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#") and "=" in line}))


if __name__ == "__main__":
    unittest.main()
