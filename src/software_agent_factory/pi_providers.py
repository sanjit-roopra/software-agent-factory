"""Credential environment variables per pi provider.

A leaf module (constants and one pure function, no imports beyond the
standard library) so both the runtime (:mod:`~software_agent_factory.pi_runtime`,
which scrubs pi's environment) and the preflight
(:mod:`~software_agent_factory.doctor`, which checks a credential exists) read
one map instead of two that drift apart.
"""

from __future__ import annotations

#: Every environment variable pi reads to authenticate a provider, per pi
#: provider name. Names and variables were read from the pi 0.84.4 bundle.
#: A provider missing here is not an error (``PiConfig.provider`` is a free
#: string): :func:`pi_provider_credential_vars` derives its
#: ``<PROVIDER>_API_KEY`` variable from the name instead.
PI_PROVIDER_CREDENTIAL_ENV_VARS: dict[str, tuple[str, ...]] = {
    "github-copilot": ("COPILOT_GITHUB_TOKEN",),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"),
    "amazon-bedrock": (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECRET_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    ),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY",),
    "google-vertex": ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "huggingface": ("HF_TOKEN",),
}


def pi_provider_credential_vars(provider: str) -> tuple[str, ...]:
    """Return the credential variables pi reads for ``provider``.

    A provider missing from :data:`PI_PROVIDER_CREDENTIAL_ENV_VARS` gets the
    single variable derived from its name (``xai`` -> ``XAI_API_KEY``).
    """
    known = PI_PROVIDER_CREDENTIAL_ENV_VARS.get(provider)
    if known is not None:
        return known
    return (f"{provider.upper().replace('-', '_')}_API_KEY",)
