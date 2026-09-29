from __future__ import annotations

import pytest

from software_agent_factory.pi_providers import (
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


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("xai", "XAI_API_KEY"), ("azure-openai-responses", "AZURE_OPENAI_RESPONSES_API_KEY")],
)
def test_unknown_provider_derives_its_api_key_variable_from_its_name(
    provider: str, expected: str
) -> None:
    assert provider not in PI_PROVIDER_CREDENTIAL_ENV_VARS
    assert pi_provider_credential_vars(provider) == (expected,)
