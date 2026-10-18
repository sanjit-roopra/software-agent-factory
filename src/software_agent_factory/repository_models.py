"""Repository profile, toolchain, setup and work item models."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from software_agent_factory.model_base import (
    Complexity,
    ModelBase,
    NonBlankStr,
    Risk,
    UtcDateTime,
    VersionedModel,
    _without_blank_entries,
    utc_now,
)


class RepositoryTechnology(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    REACT = "react"
    VITE = "vite"


class RepositoryTestTool(StrEnum):
    PYTEST = "pytest"
    VITEST = "vitest"


class RepositoryPackageManager(StrEnum):
    UV = "uv"
    PIP = "pip"
    POETRY = "poetry"
    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"
    BUN = "bun"


class DependencyEcosystem(StrEnum):
    PYTHON = "python"
    NPM = "npm"


class RepositoryDependency(ModelBase):
    """One direct dependency declaration retained with its version evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ecosystem: DependencyEcosystem
    name: str = Field(min_length=1, max_length=200)
    declared_version: str = Field(min_length=1, max_length=500)
    resolved_version: str | None = Field(default=None, max_length=200)
    manifest_path: str = Field(min_length=1, max_length=1000)
    resolution_path: str | None = Field(default=None, max_length=1000)
    group: str = Field(min_length=1, max_length=100)


class RepositoryProfile(VersionedModel):
    """Deterministic, read-only repository facts about one worktree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detector_version: Literal[2] = 2
    manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    shape_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    markers: tuple[str, ...] = ()
    version_files: tuple[str, ...] = ()
    technologies: tuple[RepositoryTechnology, ...] = ()
    test_tools: tuple[RepositoryTestTool, ...] = ()
    package_managers: tuple[RepositoryPackageManager, ...] = ()
    dependencies: tuple[RepositoryDependency, ...] = Field(default=(), max_length=200)
    warnings: tuple[str, ...] = ()


MAX_PACKAGE_SCRIPTS = 20
MAX_REJECTED_COMMANDS = 40
MAX_COMMAND_NOTES = 20
MAX_COMMAND_TEXT_LENGTH = 500


class ToolchainLane(StrEnum):
    """A language lane in the toolchain registry."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"


class ToolchainSlot(StrEnum):
    """One job a toolchain provider does for a lane."""

    FORMAT = "format"
    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"


class ToolchainProvider(StrEnum):
    """A recognized tool that can fill a toolchain slot."""

    RUFF = "ruff"
    BLACK = "black"
    FLAKE8 = "flake8"
    PYLINT = "pylint"
    MYPY = "mypy"
    PYRIGHT = "pyright"
    PYTEST = "pytest"
    PRETTIER = "prettier"
    BIOME = "biome"
    ESLINT = "eslint"
    OXLINT = "oxlint"
    TSC = "tsc"
    VITEST = "vitest"
    JEST = "jest"


class ToolchainSlotBinding(ModelBase):
    """The provider found for one lane slot, or the default to add when none is found."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lane: ToolchainLane
    slot: ToolchainSlot
    provider: ToolchainProvider | None = None
    default_provider: ToolchainProvider
    evidence: tuple[str, ...] = Field(default=(), max_length=40)

    @model_validator(mode="after")
    def _provider_matches_evidence(self) -> Self:
        if (self.provider is None) != (not self.evidence):
            raise ValueError("a bound provider needs evidence, and a missing one has none")
        return self

    @property
    def is_missing(self) -> bool:
        return self.provider is None


class ToolchainInventory(VersionedModel):
    """Deterministic inventory of the repository toolchain (ADR-034).

    ``complete`` is false when the profile evidence or the root configuration
    evidence was cut short, unreadable or degraded. A missing binding is then
    not proof that the repository lacks the tool.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    registry_version: Literal[1] = 1
    lanes: tuple[ToolchainLane, ...] = ()
    bindings: tuple[ToolchainSlotBinding, ...] = ()
    complete: bool = True
    package_json_scripts: tuple[str, ...] = Field(default=(), max_length=MAX_PACKAGE_SCRIPTS)
    self_targeting_providers: tuple[ToolchainProvider, ...] = ()
    #: Lanes whose mutation tool is already declared or configured.
    mutation_tool_lanes: tuple[ToolchainLane, ...] = ()
    #: Whether the root holds ``pnpm-workspace.yaml``.
    pnpm_workspace: bool = False
    #: Whether the project uses Yarn 2 or later: a root ``.yarnrc.yml``, a
    #: ``packageManager`` of ``yarn@2`` or later, or a Yarn 2 ``yarn.lock``.
    yarn_berry: bool = False
    #: Whether the root ``package.json`` declares ``workspaces``.
    yarn_workspace: bool = False
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _bindings_match_lanes(self) -> Self:
        keys = [(binding.lane, binding.slot) for binding in self.bindings]
        if len(keys) != len(set(keys)):
            raise ValueError("each lane slot can have only one binding")
        if any(binding.lane not in self.lanes for binding in self.bindings):
            raise ValueError("every binding lane must be an inventory lane")
        return self

    def binding(self, lane: ToolchainLane, slot: ToolchainSlot) -> ToolchainSlotBinding | None:
        """Return the binding for ``lane`` and ``slot``, if the slot applies."""
        return next(
            (b for b in self.bindings if b.lane is lane and b.slot is slot),
            None,
        )


class RepositoryCommandsSource(StrEnum):
    """Where the repository commands for a run come from (ADR-034)."""

    CONFIG = "config"
    DERIVED = "derived"
    NONE = "none"


class RejectedCommand(ModelBase):
    """A derived command that the factory did not use, with the reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str = Field(min_length=1, max_length=MAX_COMMAND_TEXT_LENGTH)
    reason: str = Field(min_length=1, max_length=MAX_COMMAND_TEXT_LENGTH)


class RepositoryCommandsPlan(VersionedModel):
    """The install, verify and build commands one run uses (ADR-034).

    Commands from the YAML configuration always win. Otherwise the factory
    derives commands from the toolchain inventory and keeps only those that
    pass on the unchanged base commit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: RepositoryCommandsSource
    install: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()
    build: tuple[str, ...] = ()
    rejected: tuple[RejectedCommand, ...] = Field(default=(), max_length=MAX_REJECTED_COMMANDS)
    notes: tuple[str, ...] = Field(default=(), max_length=MAX_COMMAND_NOTES)

    @model_validator(mode="after")
    def _commands_match_source(self) -> Self:
        if self.source is RepositoryCommandsSource.NONE and (
            self.install or self.verify or self.build
        ):
            raise ValueError("a plan without a source has no commands")
        if self.source is RepositoryCommandsSource.DERIVED and (not self.verify or self.build):
            raise ValueError("a derived plan has verify commands and no build commands")
        if self.source is RepositoryCommandsSource.CONFIG and self.rejected:
            raise ValueError("a configured plan rejects no commands")
        return self


class ToolchainSetupPlan(VersionedModel):
    """The tools a setup run adds to a repository (ADR-034).

    Each command adds missing development dependencies with the lane's own
    package manager, so the manifest and the lockfile change together. The
    plan never replaces a tool that the repository already has.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    commands: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    #: Repository files the setup writes: AGENTS.md, skills and their links.
    files: tuple[str, ...] = ()
    notes: tuple[str, ...] = Field(default=(), max_length=MAX_COMMAND_NOTES)

    @model_validator(mode="after")
    def _packages_match_commands(self) -> Self:
        if bool(self.commands) != bool(self.packages):
            raise ValueError("a setup plan has packages exactly when it has commands")
        return self

    @property
    def is_empty(self) -> bool:
        return not self.commands and not self.files


class SetupState(VersionedModel):
    """The last setup decision for one source repository (ADR-034).

    The trigger plans again only when the source HEAD changes, and it does not
    open a second pull request for commands and files it already proposed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    head_commit: str = Field(min_length=1, max_length=200)
    commands: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    pull_request_url: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=MAX_COMMAND_TEXT_LENGTH)


class MutationStatus(StrEnum):
    PASSED = "passed"
    NO_KILL = "no_kill"
    SKIPPED = "skipped"


class MutationReport(VersionedModel):
    """The result of the mutation gate for one verification (ADR-034).

    ``no_kill`` means the tests kill no mutant of a changed module. That is
    evidence for review, not a failure: a mutant can be equivalent to the code.
    ``skipped`` means the gate could not judge, and the reason says why.
    ``survived`` lists at most 50 mutant names. ``survived_count`` counts all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MutationStatus
    modules: tuple[str, ...] = ()
    killed: int = Field(default=0, ge=0)
    survived: tuple[str, ...] = Field(default=(), max_length=50)
    survived_count: int = Field(default=0, ge=0)
    no_tests: int = Field(default=0, ge=0)
    other: int = Field(default=0, ge=0)
    reason: str | None = Field(default=None, max_length=MAX_COMMAND_TEXT_LENGTH)


class WorkItem(VersionedModel):
    id: str = Field(min_length=1)
    external_id: str | None = None
    source: Literal["MANUAL", "GITHUB"] = "MANUAL"
    title: NonBlankStr = Field(min_length=1)
    description: NonBlankStr = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    priority: str | None = None
    #: No longer used; triage sets complexity and risk. Kept so old work item files load.
    complexity: Complexity | None = None
    risk: Risk | None = None
    project_id: str | None = None
    project_task_id: int | None = Field(default=None, ge=1)
    depends_on: list[int] = Field(default_factory=list)
    created_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_entries(cls, data: object) -> object:
        return _without_blank_entries(data, ("acceptance_criteria", "constraints"))

    @model_validator(mode="after")
    def _validate_project_linkage(self) -> WorkItem:
        if self.project_task_id is not None and self.project_id is None:
            raise ValueError("project_task_id requires project_id")
        if self.depends_on and self.project_task_id is None:
            raise ValueError("depends_on requires project_task_id")
        if self.project_task_id is not None and any(
            dependency >= self.project_task_id for dependency in self.depends_on
        ):
            raise ValueError("work item dependencies must reference earlier project task ids")
        return self
