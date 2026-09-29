from __future__ import annotations

import pytest

from software_agent_factory.pi_providers import (
    API_KEY_SUFFIX,
    PI_PROVIDER_CREDENTIAL_ENV_VARS,
    pi_provider_credential_vars,
)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("github-copilot", ("COPILOT_GITHUB_TOKEN",)),
        (
            "anthropic",
            ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"),
        ),
        ("openai", ("OPENAI_API_KEY",)),
        ("google", ("GEMINI_API_KEY",)),
        ("openrouter", ("OPENROUTER_API_KEY",)),
        ("huggingface", ("HF_TOKEN",)),
    ],
)
def test_known_provider_lists_every_variable_pi_reads_for_it(
    provider: str, expected: tuple[str, ...]
) -> None:
    assert pi_provider_credential_vars(provider) == expected


def test_amazon_bedrock_lists_the_aws_credential_variables() -> None:
    assert set(pi_provider_credential_vars("amazon-bedrock")) == {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECRET_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    }


def test_google_vertex_lists_its_application_credentials_and_api_key() -> None:
    assert set(pi_provider_credential_vars("google-vertex")) == {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_API_KEY",
    }


#: pi 0.84.4's provider -> API-key variable table, for providers whose variable
#: is not ``<PROVIDER>_API_KEY`` (``google`` reads ``GEMINI_API_KEY``, ...).
_PI_API_KEY_VARS_NOT_DERIVABLE_FROM_NAME = {
    "ant-ling": "ANT_LING_API_KEY",
    "azure-openai-responses": "AZURE_OPENAI_API_KEY",
    "cloudflare-ai-gateway": "CLOUDFLARE_API_KEY",
    "cloudflare-workers-ai": "CLOUDFLARE_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY",
    "moonshotai-cn": "MOONSHOT_API_KEY",
    "opencode": "OPENCODE_API_KEY",
    "opencode-go": "OPENCODE_API_KEY",
    "qwen-token-plan": "QWEN_TOKEN_PLAN_API_KEY",
    "qwen-token-plan-individual": "QWEN_TOKEN_PLAN_API_KEY",
    "qwen-token-plan-cn": "QWEN_TOKEN_PLAN_CN_API_KEY",
    "vercel-ai-gateway": "AI_GATEWAY_API_KEY",
    "xiaomi-token-plan-ams": "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "xiaomi-token-plan-cn": "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "xiaomi-token-plan-sgp": "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
}

#: pi 0.84.4 providers whose variable is ``<PROVIDER>_API_KEY``.
_PI_API_KEY_PROVIDERS_NAMED_AFTER_THEIR_VARIABLE = (
    "baseten",
    "cerebras",
    "deepseek",
    "fireworks",
    "groq",
    "minimax",
    "minimax-cn",
    "mistral",
    "nvidia",
    "radius",
    "together",
    "xai",
    "xiaomi",
    "zai",
    "zai-coding-cn",
)


@pytest.mark.parametrize(
    ("provider", "expected"), sorted(_PI_API_KEY_VARS_NOT_DERIVABLE_FROM_NAME.items())
)
def test_provider_whose_key_variable_differs_from_its_name_lists_the_variable_pi_reads(
    provider: str, expected: str
) -> None:
    assert pi_provider_credential_vars(provider) == (expected,)


@pytest.mark.parametrize("provider", _PI_API_KEY_PROVIDERS_NAMED_AFTER_THEIR_VARIABLE)
def test_provider_named_after_its_key_variable_is_listed_in_the_table(provider: str) -> None:
    assert PI_PROVIDER_CREDENTIAL_ENV_VARS[provider] == (
        f"{provider.upper().replace('-', '_')}{API_KEY_SUFFIX}",
    )


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("acme-labs", "ACME_LABS_API_KEY"), ("newprovider", "NEWPROVIDER_API_KEY")],
)
def test_unknown_provider_derives_its_api_key_variable_from_its_name(
    provider: str, expected: str
) -> None:
    assert provider not in PI_PROVIDER_CREDENTIAL_ENV_VARS
    assert pi_provider_credential_vars(provider) == (expected,)
