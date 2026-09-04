from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)

from .models import Complexity, Risk

DEFAULT_CONFIG_FILENAME = "default_config.yaml"

DEFAULT_AGENT_TIMEOUT_SECONDS = 900
DEFAULT_LOG_CAPTURE_BYTES = 32768
DEFAULT_MAX_CHANGED_FILES = 100

#: Files that must never be modified by an implementation attempt. Matched as
#: glob patterns against repository-relative changed-file paths.
DEFAULT_PROTECTED_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/id_rsa",
    "**/id_ed25519",
    "**/.npmrc",
    "**/.netrc",
    "**/.pypirc",
    "**/.git-credentials",
    "**/credentials.json",
    "**/secrets.json",
    "**/secrets.yaml",
    "**/secrets.yml",
    "**/.aws/**",
    "**/.ssh/**",
)

#: Maximum scheduler concurrency the factory is validated for (PLAN.md Phase
#: 14 deliberately stops at two concurrent tasks).
MAX_SUPPORTED_CONCURRENT_TASKS = 2


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryConfig(ConfigModel):
    same_model_attempts: PositiveInt
    max_total_attempts: PositiveInt

    @model_validator(mode="after")
    def _validate_budget(self) -> Self:
        if self.max_total_attempts < self.same_model_attempts:
            raise ValueError(
                "max_total_attempts must be greater than or equal to same_model_attempts"
            )
        return self


class RoleModelConfig(ConfigModel):
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)

    @property
    def model_family(self) -> str:
        return self.model.split("-", 1)[0].lower()


class ModelsConfig(ConfigModel):
    triage: RoleModelConfig
    refiner: RoleModelConfig
    researcher: RoleModelConfig
    planner: RoleModelConfig
    tester: RoleModelConfig
    reviewer: RoleModelConfig
    workers: dict[Complexity, RoleModelConfig]

    @model_validator(mode="after")
    def _validate_workers(self) -> Self:
        if set(self.workers) != set(Complexity):
            raise ValueError("workers must define exactly L0, L1, L2, and L3")

        reviewer_family = self.reviewer.model_family
        worker_families = {config.model_family for config in self.workers.values()}
        if reviewer_family in worker_families:
            raise ValueError("reviewer model family must differ from all worker model families")

        return self


class RepositoryCommandsConfig(ConfigModel):
    install: list[str] = Field(default_factory=list)
    verify: list[str] = Field(default_factory=list)
    build: list[str] = Field(default_factory=list)


class RepositoryConfig(ConfigModel):
    branch_prefix: str = Field(min_length=1)
    command_timeout_seconds: PositiveInt
    commands: RepositoryCommandsConfig = Field(default_factory=RepositoryCommandsConfig)
    env_passthrough: list[str] = Field(
        default_factory=list,
        description=(
            "Extra environment variable names repository commands may see, on top of "
            "the verifier's minimal allowlist. Credentials are never passed through "
            "implicitly."
        ),
    )
    log_capture_bytes: PositiveInt = DEFAULT_LOG_CAPTURE_BYTES
    max_changed_files: PositiveInt = DEFAULT_MAX_CHANGED_FILES
    protected_file_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROTECTED_FILE_PATTERNS)
    )

    @field_validator("env_passthrough")
    @classmethod
    def _validate_env_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name or not name.strip() or name != name.strip():
                raise ValueError("env_passthrough entries must be non-empty variable names")
            if "=" in name:
                raise ValueError("env_passthrough entries must be names, not assignments")
        return value

    @field_validator("protected_file_patterns")
    @classmethod
    def _validate_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not pattern.strip():
                raise ValueError("protected_file_patterns entries must be non-empty")
        return value


class ScopeDriftConfig(ConfigModel):
    """Scope-drift policy (``PLAN.md`` Phase 9).

    ``max_replans`` bounds how many times a scope-drift finding may send a run
    back to planning before it must escalate to a human.
    """

    max_replans: NonNegativeInt = 1


class PullRequestConfig(ConfigModel):
    """Pull request creation policy (``PLAN.md`` Phase 10). Never merges."""

    enabled: bool = False
    remote: str = Field(default="origin", min_length=1)
    base_branch: str | None = None
    draft: bool = True
    allowed_hosts: list[str] = Field(default_factory=lambda: ["github.com"])

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_hosts(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("allowed_hosts must not be empty")
        for host in value:
            if not host.strip() or "/" in host:
                raise ValueError("allowed_hosts entries must be bare hostnames")
        return value

    @field_validator("base_branch")
    @classmethod
    def _validate_base_branch(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("base_branch must be a non-empty branch name when provided")
        return value


class CiConfig(ConfigModel):
    """CI observation and bounded CI repair (``PLAN.md`` Phases 11 and 12)."""

    enabled: bool = False
    poll_interval_seconds: PositiveInt = 30
    max_wait_seconds: PositiveInt = 1800
    repair_attempts: PositiveInt = 3

    @model_validator(mode="after")
    def _validate_wait_budget(self) -> Self:
        if self.max_wait_seconds < self.poll_interval_seconds:
            raise ValueError(
                "ci.max_wait_seconds must be greater than or equal to ci.poll_interval_seconds"
            )
        return self


#: Conservative default dispatch-rate ceiling for the local backlog daemon
#: (PLAN.md Phase 15 core safety foundation). The packaged default runtime
#: is ``fake`` (no cost either way), but ``scheduler.enabled`` and
#: ``--runtime`` are independent knobs: a user may enable the daemon and
#: separately opt into ``--runtime copilot`` (a real, paid model call per
#: run), so this bound must be safe even then, not merely generous for
#: offline/local development. Set to ``null`` to opt out of a daily cap
#: entirely.
DEFAULT_MAX_RUNS_PER_DAY = 20


class SchedulerConfig(ConfigModel):
    """Local backlog daemon policy (``PLAN.md`` Phases 13 and 14)."""

    enabled: bool = False
    poll_interval_seconds: PositiveInt = 30
    max_concurrent_tasks: PositiveInt = 1
    stall_timeout_seconds: PositiveInt = 900
    required_label: str = Field(default="agent-ready", min_length=1)
    max_runs_per_day: PositiveInt | None = DEFAULT_MAX_RUNS_PER_DAY

    @model_validator(mode="after")
    def _validate_concurrency(self) -> Self:
        if self.max_concurrent_tasks > MAX_SUPPORTED_CONCURRENT_TASKS:
            raise ValueError(
                "scheduler.max_concurrent_tasks must be 1 or "
                f"{MAX_SUPPORTED_CONCURRENT_TASKS}"
            )
        if self.stall_timeout_seconds < self.poll_interval_seconds:
            raise ValueError(
                "scheduler.stall_timeout_seconds must be greater than or equal to "
                "scheduler.poll_interval_seconds"
            )
        return self


class RiskRuleConfig(ConfigModel):
    human_approval: bool


class FactorySettings(ConfigModel):
    data_dir: Path
    retries: RetryConfig
    agent_timeout_seconds: PositiveInt = DEFAULT_AGENT_TIMEOUT_SECONDS

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: str | Path) -> Path:
        return Path(value).expanduser()


class FactoryConfig(ConfigModel):
    factory: FactorySettings
    models: ModelsConfig
    repository: RepositoryConfig
    risk: dict[Risk, RiskRuleConfig]
    scope_drift: ScopeDriftConfig = Field(default_factory=ScopeDriftConfig)
    pull_request: PullRequestConfig = Field(default_factory=PullRequestConfig)
    ci: CiConfig = Field(default_factory=CiConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)

    @model_validator(mode="after")
    def _validate_risk_rules(self) -> Self:
        if set(self.risk) != set(Risk):
            raise ValueError("risk must define exactly R0, R1, R2, and R3")
        return self

    @model_validator(mode="after")
    def _validate_ci_requires_pull_request(self) -> Self:
        if self.ci.enabled and not self.pull_request.enabled:
            raise ValueError("ci.enabled requires pull_request.enabled")
        return self

    @property
    def data_dir(self) -> Path:
        return self.factory.data_dir

    @property
    def retries(self) -> RetryConfig:
        return self.factory.retries

    @property
    def agent_timeout_seconds(self) -> int:
        return self.factory.agent_timeout_seconds


def load_config(path: str | Path | None = None) -> FactoryConfig:
    if path is None:
        raw_text = (
            resources.files("software_agent_factory")
            .joinpath(DEFAULT_CONFIG_FILENAME)
            .read_text(encoding="utf-8")
        )
    else:
        raw_text = Path(path).expanduser().read_text(encoding="utf-8")

    payload = yaml.safe_load(raw_text)
    if not isinstance(payload, dict):
        raise ValueError("Configuration must be a YAML mapping")

    return FactoryConfig.model_validate(payload)
