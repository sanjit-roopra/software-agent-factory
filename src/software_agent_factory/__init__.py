"""Local-first autonomous software engineering factory (Phases 1-14)."""

from .agents import AgentRequest, AgentResult, AgentRuntime, FakeAgentRuntime
from .config import FactoryConfig, load_config
from .copilot_runtime import CopilotAgentRuntime
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
from .publishing import CIObserver, PullRequestPublisher
from .routing import ModelRouter
from .scheduler import Scheduler, TrackerItem, TrackerProvider, deterministic_work_item_id
from .service import FactoryService
from .store import FileRunStore
from .verification import DeterministicVerifier
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
    "CheckStatus",
    "CommandResult",
    "Complexity",
    "CopilotAgentRuntime",
    "DeterministicVerifier",
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
    "ModelRouter",
    "PlanStep",
    "PublishGate",
    "PullRequestPublisher",
    "RepairContext",
    "RepositoryVerifier",
    "ResearchReport",
    "ReviewReport",
    "Risk",
    "RunLease",
    "Scheduler",
    "ScopeAssessment",
    "ScopeDecision",
    "ScopeDriftPolicy",
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
    "build_pr_body",
    "deterministic_work_item_id",
    "is_run_finished",
    "load_config",
]

__version__ = "0.1.0"
