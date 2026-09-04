"""Local-first autonomous software engineering factory (Phases 1-15).

Re-exports the stable domain surface: typed artifacts, the workflow
controller, governance, the run store, the agent runtimes, and the Phase 15
operational modules (preflight, monitoring/health, launchd service lifecycle
and version resolution).

Two things are deliberately *not* re-exported here:

- ``cli`` -- importing the CLI would pull Typer and every command wiring into
  a plain library import, and would make this module part of an import cycle.
- ``dashboard`` -- the read-only viewer (ADR-016) is a self-contained
  subpackage reached through ``software_agent_factory.dashboard``. Keeping it
  out of the top-level namespace mirrors its runtime rule: it exists only
  when something explicitly asks for it.
"""

from .agents import AgentRequest, AgentResult, AgentRuntime, FakeAgentRuntime
from .config import FactoryConfig, load_config
from .copilot_runtime import CopilotAgentRuntime
from .doctor import (
    CheckResult,
    DoctorEnvironment,
    DoctorReport,
    missing_prerequisites,
    requires_gh,
    run_doctor,
)
from .github import (
    CheckStatus,
    CIStatus,
    FailureCategory,
    GitHubClient,
    GitPublisher,
    build_pr_body,
)
from .github_tracker import GitHubIssueProvider
from .governance import (
    PublishGate,
    RepositoryVerifier,
    ScopeAssessment,
    ScopeDecision,
    ScopeDriftPolicy,
    assess_publish_gate,
)
from .models import (
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    ChangeSet,
    CICheckEvidence,
    CIReport,
    CommandResult,
    Complexity,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    PlanStep,
    RepairContext,
    ResearchReport,
    ReviewReport,
    Risk,
    RunLease,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkflowState,
    WorkItem,
)
from .observability import (
    MonitoringSnapshot,
    OperationalHealthReport,
    RunDetail,
    RunSummary,
    build_monitoring_snapshot,
    build_operational_health,
    build_run_detail,
    configure_factory_logging,
    log_run_event,
)
from .publishing import CIObserver, PullRequestPublisher
from .routing import ModelRouter
from .scheduler import Scheduler, TrackerItem, TrackerProvider, deterministic_work_item_id
from .service import FactoryService
from .service_install import (
    ServiceInstallError,
    ServiceInstallRequest,
    ServiceRuntime,
    ServiceStatus,
    get_service_status,
    install_service,
    resolve_factory_executable,
    uninstall_service,
)
from .store import FileRunStore, InvalidRunIdError
from .verification import DeterministicVerifier
from .version import format_version_line, get_build_info, get_version
from .workflow import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    TransitionError,
    WorkflowController,
    is_run_finished,
)
from .workspace import GitWorktreeWorkspace

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AgentRequest",
    "AgentResult",
    "AgentRole",
    "AgentRuntime",
    "AttemptBudget",
    "AttemptRecord",
    "AttemptTrigger",
    "CICheckEvidence",
    "CIObserver",
    "CIReport",
    "CIStatus",
    "ChangeSet",
    "CheckResult",
    "CheckStatus",
    "CommandResult",
    "Complexity",
    "CopilotAgentRuntime",
    "DeterministicVerifier",
    "DoctorEnvironment",
    "DoctorReport",
    "ExecutionPlan",
    "ExpectedScope",
    "FactoryConfig",
    "FactoryRun",
    "FactoryService",
    "FailureCategory",
    "FakeAgentRuntime",
    "FileRunStore",
    "GitHubClient",
    "GitHubIssueProvider",
    "GitPublisher",
    "GitWorktreeWorkspace",
    "InvalidRunIdError",
    "ModelRouter",
    "MonitoringSnapshot",
    "OperationalHealthReport",
    "PlanStep",
    "PublishGate",
    "PullRequestPublisher",
    "RepairContext",
    "RepositoryVerifier",
    "ResearchReport",
    "ReviewReport",
    "Risk",
    "RunDetail",
    "RunLease",
    "RunSummary",
    "Scheduler",
    "ScopeAssessment",
    "ScopeDecision",
    "ScopeDriftPolicy",
    "ServiceInstallError",
    "ServiceInstallRequest",
    "ServiceRuntime",
    "ServiceStatus",
    "Specification",
    "TERMINAL_STATES",
    "TestReport",
    "TrackerItem",
    "TrackerProvider",
    "TransitionError",
    "TriageResult",
    "VerificationReport",
    "WorkItem",
    "WorkflowController",
    "WorkflowState",
    "assess_publish_gate",
    "build_monitoring_snapshot",
    "build_operational_health",
    "build_pr_body",
    "build_run_detail",
    "configure_factory_logging",
    "deterministic_work_item_id",
    "format_version_line",
    "get_build_info",
    "get_service_status",
    "get_version",
    "install_service",
    "is_run_finished",
    "load_config",
    "log_run_event",
    "missing_prerequisites",
    "requires_gh",
    "resolve_factory_executable",
    "run_doctor",
    "uninstall_service",
]

#: Kept in sync with ``pyproject.toml``; ``version.get_version()`` is the
#: authoritative resolver (it also understands installed metadata and a
#: frozen build's bundled ``build-info.json``).
__version__ = "0.1.0"
