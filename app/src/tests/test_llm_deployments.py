"""Focused tests for named Arvan deployment configuration and transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import requests

import src.config.settings as settings_module
from src.config.llm_deployments import normalize_chat_endpoint
from src.config.settings import Settings, get_settings
from src.core.context.entities import EntityResolver
from src.core.context.intent import GLMIntentRouter
from src.core.llm.client import LLMClient
from src.core.llm.errors import LLMError
from src.core.memory.routing_state import SessionRoutingState


GLM_BASE_URL = "https://glm.example.invalid/v1"
GPT_BASE_URL = "https://gpt.example.invalid/v1"
GLM_ENDPOINT = f"{GLM_BASE_URL}/chat/completions"
GPT_ENDPOINT = f"{GPT_BASE_URL}/chat/completions"
GLM_KEY = "fixture-glm-key"
GPT_KEY = "fixture-gpt-key"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object | None = None, *, invalid_json: bool = False) -> None:
        self.status_code = status_code
        self.payload = payload
        self.invalid_json = invalid_json

    def json(self) -> object:
        if self.invalid_json:
            raise ValueError("invalid JSON fixture")
        return self.payload if self.payload is not None else {}


def response(
    text: str = "ok",
    *,
    model: str = "fixture-model",
    finish_reason: str | None = "stop",
    usage: object | None = None,
    reasoning: str | None = None,
) -> FakeResponse:
    message: dict[str, object] = {"content": text}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    payload: dict[str, object] = {
        "model": model,
        "choices": [{"message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return FakeResponse(payload=payload)


def isolated_settings(**overrides: str) -> Settings:
    environment = {
        "SOORIN_LLM_ENABLED": "true",
        "SOORIN_LLM_PROVIDER": "arvan",
        "SOORIN_LLM_GLM_BASE_URL": GLM_BASE_URL,
        "SOORIN_LLM_GLM_CHAT_PATH": "/chat/completions",
        "SOORIN_LLM_GLM_MODEL": "GLM-5.2",
        "SOORIN_LLM_GLM_API_KEY": GLM_KEY,
        "SOORIN_LLM_GLM_AUTH_SCHEME": "apikey",
        "SOORIN_LLM_GLM_CONNECT_TIMEOUT_SECONDS": "8",
        "SOORIN_LLM_GLM_ROUTER_TIMEOUT_SECONDS": "15",
        "SOORIN_LLM_GLM_CHAT_TIMEOUT_SECONDS": "300",
        "SOORIN_LLM_GLM_MAX_TOKENS": "12288",
        "SOORIN_LLM_GLM_ROUTER_MAX_TOKENS": "384",
        "SOORIN_LLM_GLM_ROUTER_RETRY_MAX_TOKENS": "640",
        "SOORIN_LLM_GLM_CHAT_MAX_TOKENS": "4096",
        "SOORIN_LLM_GLM_ROUTER_TEMPERATURE": "0.0",
        "SOORIN_LLM_GLM_ROUTER_TOP_P": "0.1",
        "SOORIN_LLM_GLM_CHAT_TEMPERATURE": "0.2",
        "SOORIN_LLM_GLM_CHAT_TOP_P": "0.9",
        "SOORIN_LLM_GLM_SUPPORTS_TEMPERATURE": "true",
        "SOORIN_LLM_GLM_SUPPORTS_TOP_P": "true",
        "SOORIN_LLM_GPT55_BASE_URL": GPT_BASE_URL,
        "SOORIN_LLM_GPT55_CHAT_PATH": "/chat/completions",
        "SOORIN_LLM_GPT55_MODEL": "GPT-5.5",
        "SOORIN_LLM_GPT55_API_KEY": GPT_KEY,
        "SOORIN_LLM_GPT55_AUTH_SCHEME": "Bearer",
        "SOORIN_LLM_MAX_TRANSIENT_RETRIES": "1",
        "SOORIN_LLM_RETRY_BASE_DELAY_SECONDS": "0",
        "SOORIN_LLM_RETRY_MAX_DELAY_SECONDS": "0",
    }
    environment.update(overrides)
    missing_env = Path("/tmp/soorin-llm-deployment-tests-missing.env")
    with patch.dict(os.environ, environment, clear=True), patch.object(settings_module, "ENV_PATH", missing_env):
        get_settings.cache_clear()
        try:
            return get_settings()
        finally:
            get_settings.cache_clear()


class EndpointNormalizerTests(unittest.TestCase):
    def test_glm_and_gpt_v1_base_urls_resolve_to_chat_completions(self) -> None:
        self.assertEqual(normalize_chat_endpoint(GLM_BASE_URL, "/chat/completions"), GLM_ENDPOINT)
        self.assertEqual(normalize_chat_endpoint(GPT_BASE_URL, "/chat/completions"), GPT_ENDPOINT)

    def test_existing_chat_endpoint_is_idempotent(self) -> None:
        self.assertEqual(normalize_chat_endpoint(GLM_ENDPOINT, "/chat/completions"), GLM_ENDPOINT)

    def test_trailing_slashes_and_missing_leading_path_slash_are_normalized(self) -> None:
        self.assertEqual(
            normalize_chat_endpoint(f"{GLM_BASE_URL}/", "chat/completions"),
            GLM_ENDPOINT,
        )
        self.assertEqual(
            normalize_chat_endpoint(f"{GLM_ENDPOINT}/", "/chat/completions"),
            GLM_ENDPOINT,
        )

    def test_empty_base_url_remains_empty_and_empty_path_uses_default(self) -> None:
        self.assertEqual(normalize_chat_endpoint("", "/chat/completions"), "")
        self.assertEqual(normalize_chat_endpoint(GLM_BASE_URL, ""), GLM_ENDPOINT)


class DeploymentConfigurationTests(unittest.TestCase):
    def test_symmetric_environment_and_absent_selectors_default_to_glm(self) -> None:
        settings = isolated_settings(SOORIN_LLM_GPT55_BASE_URL="", SOORIN_LLM_GPT55_API_KEY="")

        self.assertEqual(settings.intent_router_deployment, "glm")
        self.assertEqual(settings.chat_deployment, "glm")
        self.assertEqual(settings.deployment("glm").endpoint, GLM_ENDPOINT)
        self.assertEqual(settings.deployment("glm").model, "GLM-5.2")

    def test_both_selectors_can_use_gpt55(self) -> None:
        settings = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="gpt55",
            SOORIN_CHAT_DEPLOYMENT="gpt55",
        )

        self.assertEqual(settings.deployment_for_purpose("intent_router").name, "gpt55")
        self.assertEqual(settings.deployment_for_purpose("chat").name, "gpt55")

    def test_mixed_router_and_chat_selection_is_independent(self) -> None:
        glm_router = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="glm",
            SOORIN_CHAT_DEPLOYMENT="gpt55",
        )
        gpt_router = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="gpt55",
            SOORIN_CHAT_DEPLOYMENT="glm",
        )

        self.assertEqual(glm_router.deployment_for_purpose("intent_router").name, "glm")
        self.assertEqual(glm_router.deployment_for_purpose("chat").name, "gpt55")
        self.assertEqual(gpt_router.deployment_for_purpose("intent_router").name, "gpt55")
        self.assertEqual(gpt_router.deployment_for_purpose("chat").name, "glm")

    def test_profiles_expose_base_url_chat_path_and_shared_normalized_endpoint(self) -> None:
        settings = isolated_settings(
            SOORIN_LLM_GLM_BASE_URL="https://glm-alias.example.invalid/v1/",
            SOORIN_LLM_GLM_CHAT_PATH="chat/completions",
        )
        glm = settings.deployment("glm")
        gpt = settings.deployment("gpt55")

        self.assertEqual(glm.base_url, "https://glm-alias.example.invalid/v1")
        self.assertEqual(glm.chat_path, "chat/completions")
        self.assertEqual(glm.endpoint, normalize_chat_endpoint(glm.base_url, glm.chat_path))
        self.assertEqual(gpt.endpoint, normalize_chat_endpoint(gpt.base_url, gpt.chat_path))

    def test_invalid_alias_fails_with_safe_valid_alias_list(self) -> None:
        with self.assertRaisesRegex(ValueError, r"Valid aliases: glm, gpt55") as raised:
            isolated_settings(SOORIN_CHAT_DEPLOYMENT="unknown-secret-value")

        self.assertNotIn(GLM_KEY, str(raised.exception))
        self.assertNotIn(GPT_KEY, str(raised.exception))

    def test_missing_selected_base_url_fails_during_startup_validation(self) -> None:
        settings = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="gpt55",
            SOORIN_CHAT_DEPLOYMENT="gpt55",
            SOORIN_LLM_GPT55_BASE_URL="",
        )

        with self.assertRaisesRegex(ValueError, "not configured for: gpt55"):
            settings.validate_selected_llm_deployments()

    def test_env_example_is_symmetric_secret_free_and_has_no_duplicates_or_obsolete_keys(self) -> None:
        text = (settings_module.APP_DIR / ".env.example").read_text(encoding="utf-8")
        keys: list[str] = []
        values: dict[str, str] = {}
        for line in text.splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                keys.append(key.strip())
                values[key.strip()] = value.strip()

        for name in (
            "SOORIN_LLM_GLM_BASE_URL",
            "SOORIN_LLM_GLM_API_KEY",
            "SOORIN_LLM_GPT55_BASE_URL",
            "SOORIN_LLM_GPT55_API_KEY",
        ):
            self.assertEqual(values[name], "")
        self.assertEqual(len(keys), len(set(keys)))
        obsolete = (
            "SOORIN_ARVAN_",
            "SOORIN_LLM_GLM_ENDPOINT",
            "SOORIN_LLM_GPT55_ENDPOINT",
            "SOORIN_LLM_CONNECT_TIMEOUT_SECONDS",
            "SOORIN_CHAT_TIMEOUT_SECONDS",
            "SOORIN_CHAT_MAX_TOKENS",
            "SOORIN_INTENT_ROUTER_TIMEOUT_SECONDS",
            "SOORIN_INTENT_ROUTER_TEMPERATURE",
            "SOORIN_INTENT_ROUTER_TOP_P",
            "SOORIN_INTENT_ROUTER_MAX_TOKENS",
            "SOORIN_INTENT_ROUTER_RETRY_MAX_TOKENS",
        )
        self.assertFalse(any(key.startswith(obsolete) for key in keys))

    def test_runtime_settings_source_contains_no_obsolete_environment_names(self) -> None:
        source = Path(settings_module.__file__).read_text(encoding="utf-8")
        obsolete = (
            "SOORIN_ARVAN_",
            "SOORIN_LLM_GLM_ENDPOINT",
            "SOORIN_LLM_GPT55_ENDPOINT",
            "SOORIN_LLM_CONNECT_TIMEOUT_SECONDS",
            "SOORIN_CHAT_TIMEOUT_SECONDS",
            "SOORIN_CHAT_MAX_TOKENS",
            "SOORIN_INTENT_ROUTER_TIMEOUT_SECONDS",
            "SOORIN_INTENT_ROUTER_TEMPERATURE",
            "SOORIN_INTENT_ROUTER_TOP_P",
            "SOORIN_INTENT_ROUTER_MAX_TOKENS",
            "SOORIN_INTENT_ROUTER_RETRY_MAX_TOKENS",
        )
        for name in obsolete:
            self.assertNotIn(name, source)

    def test_health_reports_selected_deployments_without_endpoints_or_credentials(self) -> None:
        settings = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="glm",
            SOORIN_CHAT_DEPLOYMENT="gpt55",
        )

        health = LLMClient(settings).health()
        serialized = json.dumps(health, sort_keys=True)

        self.assertEqual(health["router"]["deployment"], "glm")  # type: ignore[index]
        self.assertEqual(health["chat"]["deployment"], "gpt55")  # type: ignore[index]
        self.assertNotIn(GLM_ENDPOINT, serialized)
        self.assertNotIn(GPT_ENDPOINT, serialized)
        self.assertNotIn(GLM_KEY, serialized)
        self.assertNotIn(GPT_KEY, serialized)


class DeploymentRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = [{"role": "user", "content": "hello"}]

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_router_and_chat_use_independently_selected_endpoints_and_models(self, post) -> None:
        settings = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="glm",
            SOORIN_CHAT_DEPLOYMENT="gpt55",
            SOORIN_LLM_GPT55_CONNECT_TIMEOUT_SECONDS="11",
            SOORIN_LLM_GPT55_CHAT_TIMEOUT_SECONDS="222",
        )
        client = LLMClient(settings)
        post.side_effect = [response("{}"), response("answer")]

        router_result = client.chat(self.messages, purpose="intent_router", transient_retries=0)
        chat_result = client.chat(self.messages, purpose="chat", transient_retries=0)

        self.assertEqual(post.call_args_list[0].args[0], GLM_ENDPOINT)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["model"], "GLM-5.2")
        self.assertEqual(post.call_args_list[1].args[0], GPT_ENDPOINT)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["model"], "GPT-5.5")
        self.assertEqual(post.call_args_list[0].kwargs["timeout"], (8, 15))
        self.assertEqual(post.call_args_list[1].kwargs["timeout"], (11, 222))
        self.assertEqual(router_result.deployment, "glm")
        self.assertEqual(chat_result.deployment, "gpt55")

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_inverse_mixed_selection_routes_gpt_router_and_glm_chat(self, post) -> None:
        settings = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="gpt55",
            SOORIN_CHAT_DEPLOYMENT="glm",
        )
        client = LLMClient(settings)
        post.side_effect = [response("{}"), response("answer")]

        client.chat(self.messages, purpose="intent_router", transient_retries=0)
        client.chat(self.messages, purpose="chat", transient_retries=0)

        self.assertEqual(post.call_args_list[0].args[0], GPT_ENDPOINT)
        self.assertEqual(post.call_args_list[1].args[0], GLM_ENDPOINT)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_router_repair_uses_router_deployment_and_repair_budget(self, post) -> None:
        settings = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="gpt55",
            SOORIN_CHAT_DEPLOYMENT="glm",
            SOORIN_LLM_GPT55_ROUTER_MAX_TOKENS="77",
            SOORIN_LLM_GPT55_ROUTER_RETRY_MAX_TOKENS="155",
        )
        client = LLMClient(settings)
        valid = json.dumps(
            {
                "intent": "asset_investigation",
                "scope": "node_summary",
                "direction": "both",
                "depth": 0,
                "requires_graph": True,
                "requires_multiple_entities": False,
                "is_followup": False,
                "classification_confidence": 0.9,
                "reason": "fixture",
            }
        )
        post.side_effect = [response("not json"), response(valid)]
        entities = EntityResolver().resolve("Tell me about 192.0.2.10")

        decision = GLMIntentRouter(settings, client).classify(
            "Tell me about 192.0.2.10",
            entities,
            SessionRoutingState(),
        )

        self.assertFalse(decision.fallback_used)
        self.assertEqual([call.args[0] for call in post.call_args_list], [GPT_ENDPOINT, GPT_ENDPOINT])
        self.assertEqual(post.call_args_list[0].kwargs["json"]["max_tokens"], 77)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["max_tokens"], 155)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_payload_authentication_and_optional_parameter_omission(self, post) -> None:
        settings = isolated_settings(
            SOORIN_INTENT_ROUTER_DEPLOYMENT="gpt55",
            SOORIN_CHAT_DEPLOYMENT="gpt55",
            SOORIN_LLM_GPT55_CHAT_MAX_TOKENS="987",
            SOORIN_LLM_GPT55_CHAT_TEMPERATURE="0.4",
            SOORIN_LLM_GPT55_CHAT_TOP_P="0.8",
            SOORIN_LLM_GPT55_SUPPORTS_TEMPERATURE="false",
            SOORIN_LLM_GPT55_SUPPORTS_TOP_P="false",
        )
        client = LLMClient(settings)
        post.return_value = response("answer")

        client.chat(self.messages, purpose="chat", transient_retries=0)

        payload = post.call_args.kwargs["json"]
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(payload["model"], "GPT-5.5")
        self.assertEqual(payload["messages"], self.messages)
        self.assertEqual(payload["max_tokens"], 987)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertEqual(headers["Authorization"], f"Bearer {GPT_KEY}")

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_glm_sampling_parameters_are_included_when_capabilities_are_enabled(self, post) -> None:
        settings = isolated_settings(
            SOORIN_CHAT_DEPLOYMENT="glm",
            SOORIN_LLM_GLM_CHAT_TEMPERATURE="0.4",
            SOORIN_LLM_GLM_CHAT_TOP_P="0.8",
            SOORIN_LLM_GLM_SUPPORTS_TEMPERATURE="true",
            SOORIN_LLM_GLM_SUPPORTS_TOP_P="true",
        )
        client = LLMClient(settings)
        post.return_value = response("answer")

        client.chat(self.messages, purpose="chat", transient_retries=0)

        self.assertEqual(post.call_args.kwargs["json"]["temperature"], 0.4)
        self.assertEqual(post.call_args.kwargs["json"]["top_p"], 0.8)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_logs_redact_credential_url_path_key_and_header(self, post) -> None:
        secret_path = "secret-path-token"
        secret_key = "fixture-log-secret-key"
        base_url = f"https://safe.example.invalid/gateway/{secret_path}/v1"
        endpoint = f"{base_url}/chat/completions"
        settings = isolated_settings(
            SOORIN_CHAT_DEPLOYMENT="gpt55",
            SOORIN_LLM_GPT55_BASE_URL=base_url,
            SOORIN_LLM_GPT55_API_KEY=secret_key,
        )
        post.return_value = response("answer")

        with self.assertLogs("src.core.llm", level="INFO") as captured:
            client = LLMClient(settings)
            client.chat(self.messages, purpose="chat", transient_retries=0)

        logs = "\n".join(captured.output)
        self.assertIn("deployment=gpt55", logs)
        self.assertIn("host=safe.example.invalid", logs)
        self.assertNotIn(endpoint, logs)
        self.assertNotIn(secret_path, logs)
        self.assertNotIn(secret_key, logs)
        self.assertNotIn("Authorization", logs)


class UsageAndErrorCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = LLMClient(isolated_settings())
        self.messages = [{"role": "user", "content": "hello"}]

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_usage_variants_content_and_reasoning_metadata_are_preserved_safely(self, post) -> None:
        post.side_effect = [
            response(
                "content",
                finish_reason="length",
                usage={"prompt_tokens": 5, "completion_tokens": 9, "output_tokens": 0, "total_tokens": 14},
                reasoning="hidden fixture reasoning",
            ),
            response("content", usage={"completion_tokens": 3}),
            response("content"),
        ]

        first = self.client.chat(self.messages, transient_retries=0)
        second = self.client.chat(self.messages, transient_retries=0)
        third = self.client.chat(self.messages, transient_retries=0)

        self.assertEqual(first.text, "content")
        self.assertEqual(first.finish_reason, "length")
        self.assertEqual(first.usage["completion_tokens"], 9)
        self.assertEqual(first.usage["output_tokens"], 0)
        self.assertTrue(first.reasoning_present)
        self.assertFalse(first.reasoning_exposed)
        self.assertNotIn("output_tokens", second.usage)
        self.assertEqual(third.usage, {})

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_request_completion_log_contains_safe_comparison_telemetry(self, post) -> None:
        post.return_value = response(
            "answer",
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            reasoning="hidden fixture reasoning",
        )

        with self.assertLogs("src.core.llm.providers.arvan", level="INFO") as captured:
            self.client.chat(self.messages, request_id="telemetry", transient_retries=0)

        logs = "\n".join(captured.output)
        for expected in (
            "event=llm_request_complete",
            "purpose=chat",
            "deployment=glm",
            "provider=arvan",
            "model=GLM-5.2",
            "status_code=200",
            "latency_ms=",
            "finish_reason=stop",
            "prompt_tokens=5",
            "completion_tokens=2",
            "total_tokens=7",
            "output_chars=6",
            "reasoning_present=True",
        ):
            self.assertIn(expected, logs)
        self.assertNotIn("hidden fixture reasoning", logs)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_http_error_statuses_remain_safe_and_do_not_cross_model_fallback(self, post) -> None:
        for status_code in (401, 404, 429, 500):
            with self.subTest(status_code=status_code):
                post.reset_mock()
                post.return_value = FakeResponse(status_code=status_code)
                with self.assertRaises(LLMError) as raised:
                    self.client.chat(self.messages, transient_retries=0)
                self.assertEqual(raised.exception.details["status_code"], status_code)
                self.assertEqual(raised.exception.details["deployment"], "glm")
                self.assertEqual(post.call_count, 1)

    @patch("src.core.llm.providers.arvan.requests.post")
    def test_timeout_malformed_json_empty_content_and_unsupported_parameter_error(self, post) -> None:
        cases = [
            (requests.exceptions.ReadTimeout(), "provider_transport_error"),
            (FakeResponse(invalid_json=True), "provider_invalid_json"),
            (response(""), "provider_empty_answer"),
            (FakeResponse(status_code=400), "provider_http_error"),
        ]
        for side_effect, reason in cases:
            with self.subTest(reason=reason):
                post.reset_mock()
                if isinstance(side_effect, Exception):
                    post.side_effect = side_effect
                    post.return_value = None
                else:
                    post.side_effect = None
                    post.return_value = side_effect
                with self.assertRaises(LLMError) as raised:
                    self.client.chat(self.messages, transient_retries=0)
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
