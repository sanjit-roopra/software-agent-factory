"""Credential environment variables per pi provider.

A leaf module (constants and one pure function, no imports beyond the
standard library) so both the runtime (:mod:`~software_agent_factory.pi_runtime`,
which scrubs pi's environment) and the preflight
(:mod:`~software_agent_factory.doctor`, which checks a credential exists) read
one map instead of two that drift apart.
"""

from __future__ import annotations

#: Suffix of the environment variable most pi providers read their API key from.
API_KEY_SUFFIX = "_API_KEY"

#: Every environment variable pi reads to authenticate a provider, per pi
#: provider name. A provider missing here is not an error
#: (``PiConfig.provider`` is a free string): :func:`pi_provider_credential_vars`
#: derives its ``<PROVIDER>_API_KEY`` variable from the name instead.
#:
#: Extracted from pi 0.84.4 (``@earendil-works/pi-coding-agent``, its ``dist``
#: bundle) by grepping for the ``<provider>:"<VAR>"`` pairs of its
#: provider-to-environment-variable table. The ``github-copilot``,
#: ``anthropic`` (OAuth and auth tokens) and ``amazon-bedrock`` (AWS chain)
#: entries also hold variables pi reads outside that table. Re-run the grep
#: when pi is upgraded.
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
    "google-vertex": ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_API_KEY"),
    "ant-ling": ("ANT_LING_API_KEY",),
    "azure-openai-responses": ("AZURE_OPENAI_API_KEY",),
    "baseten": ("BASETEN_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "cloudflare-ai-gateway": ("CLOUDFLARE_API_KEY",),
    "cloudflare-workers-ai": ("CLOUDFLARE_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "google": ("GEMINI_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "huggingface": ("HF_TOKEN",),
    "kimi-coding": ("KIMI_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "minimax-cn": ("MINIMAX_CN_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "moonshotai": ("MOONSHOT_API_KEY",),
    "moonshotai-cn": ("MOONSHOT_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "opencode": ("OPENCODE_API_KEY",),
    "opencode-go": ("OPENCODE_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "qwen-token-plan": ("QWEN_TOKEN_PLAN_API_KEY",),
    "qwen-token-plan-cn": ("QWEN_TOKEN_PLAN_CN_API_KEY",),
    "qwen-token-plan-individual": ("QWEN_TOKEN_PLAN_API_KEY",),
    "radius": ("RADIUS_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "vercel-ai-gateway": ("AI_GATEWAY_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "xiaomi": ("XIAOMI_API_KEY",),
    "xiaomi-token-plan-ams": ("XIAOMI_TOKEN_PLAN_AMS_API_KEY",),
    "xiaomi-token-plan-cn": ("XIAOMI_TOKEN_PLAN_CN_API_KEY",),
    "xiaomi-token-plan-sgp": ("XIAOMI_TOKEN_PLAN_SGP_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "zai-coding-cn": ("ZAI_CODING_CN_API_KEY",),
}


def pi_provider_credential_vars(provider: str) -> tuple[str, ...]:
    """Return the credential variables pi reads for ``provider``.

    A provider missing from :data:`PI_PROVIDER_CREDENTIAL_ENV_VARS` gets the
    single variable derived from its name (``acme-labs`` -> ``ACME_LABS_API_KEY``).
    """
    known = PI_PROVIDER_CREDENTIAL_ENV_VARS.get(provider)
    if known is not None:
        return known
    return (f"{provider.upper().replace('-', '_')}{API_KEY_SUFFIX}",)
