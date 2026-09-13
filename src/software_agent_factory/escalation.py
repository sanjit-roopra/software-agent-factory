"""Controller-owned GitHub escalation notices and authorized human reply loop.

Implements the core escalation and authorized human reply loop:
- When a run enters ``NEEDS_HUMAN``, the factory posts a concise, safe status
  comment on the persisted open factory PR if available, falling back to the
  source GitHub issue reference.
- An authorized human contributor may reply on that exact thread with:
  ``@factory resume v1 run=<run-id> episode=<opaque-id>``
- The controller validates the comment, author, timing, target, and episode,
  records a durable decision receipt, and reopens the run when the halt category
  is supported (``RISK_APPROVAL`` -> ``REFINING``).
- Comment text never enters agent prompts and cannot alter models, commands,
  paths, URLs, retry policy, or arbitrary workflow state.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from .config import FactoryConfig
from .github import (
    GitHubClient,
    GitHubComment,
    GitHubError,
    RepositoryRef,
    parse_issue_reference,
    parse_pull_request_url,
)
from .models import (
    AcceptedReplyReceipt,
    EscalationRecord,
    EscalationStatus,
    EscalationTargetType,
    FactoryRun,
    ResumeClassification,
    ReviewImpasse,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .store import FileRunStore

logger = logging.getLogger(__name__)

_RESUME_COMMAND_PATTERN = re.compile(
    r"^@factory\s+resume\s+v1\s+run=(?P<run>[A-Za-z0-9._-]+)\s+episode=(?P<episode>[A-Za-z0-9._-]+)$"
)

_ESCALATION_MARKER_TEMPLATE = (
    "<!-- software-agent-factory:escalation run={run_id} episode={episode_id} -->"
)
_ESCALATION_MARKER_REGEX = re.compile(
    r"<!--\s*software-agent-factory:escalation\s+run=(?P<run>[A-Za-z0-9._-]+)\s+episode=(?P<episode>[A-Za-z0-9._-]+)\s*-->"
)


def generate_episode_id() -> str:
    """Generate an unpredictable, cryptographically stable episode token."""
    return f"ep-{secrets.token_hex(12)}"


def format_escalation_marker(run_id: str, episode_id: str) -> str:
    """Build the stable hidden HTML marker bound to run + escalation episode."""
    return _ESCALATION_MARKER_TEMPLATE.format(run_id=run_id, episode_id=episode_id)


def parse_resume_command(body: str) -> tuple[str, str] | None:
    """Parse exact ASCII command: @factory resume v1 run=<run-id> episode=<opaque-id>."""
    cleaned = body.strip()
    match = _RESUME_COMMAND_PATTERN.fullmatch(cleaned)
    if match is None:
        return None
    return match.group("run"), match.group("episode")


class ValidationResult(tuple[bool, str]):
    """Result of candidate comment validation.

    Acts as a 2-tuple (is_valid, reason) for backward compatibility,
    with an additional `retryable` boolean indicating whether a failure
    was transient/retryable (e.g. comment re-fetch or identity resolution failure)
    versus permanent (grammar, run/episode mismatch, edited comment, unauthorized author).
    """

    is_valid: bool
    reason: str
    retryable: bool

    def __new__(
        cls,
        is_valid: bool,
        reason: str,
        *,
        retryable: bool = False,
    ) -> ValidationResult:
        instance = super().__new__(cls, (is_valid, reason))
        instance.is_valid = is_valid
        instance.reason = reason
        instance.retryable = retryable
        return instance


def classify_halt_reason(
    run: FactoryRun,
    store: FileRunStore | None = None,
) -> tuple[ResumeClassification, str, str, str]:
    """Deterministically classify a halted run into a typed resume category.

    Returns:
        (classification, reason_code, summary, next_action)
    """
    if run.state is not WorkflowState.NEEDS_HUMAN:
        return (
            ResumeClassification.NOT_RESUMABLE,
            "MANUAL_INSPECTION",
            "The run is not in NEEDS_HUMAN state.",
            "Inspect the typed run artifacts.",
        )

    if store is not None:
        try:
            impasse = store.load_artifact(run.id, ReviewImpasse)
        except (FileNotFoundError, ValueError):
            impasse = None
        if impasse is not None:
            return (
                ResumeClassification.NOT_RESUMABLE,
                "REVIEW_IMPASSE",
                "Independent review did not converge within the safe automatic policy.",
                "Inspect review-impasse.json, resolve or accept the listed findings, then retry.",
            )

    reason = (run.failure_reason or "").lower()
    if "scope" in reason:
        return (
            ResumeClassification.NOT_RESUMABLE,
            "SCOPE_REVIEW",
            "The proposed changes exceeded the approved scope.",
            "Review the planned and changed files, then update the scope or retry.",
        )
    if re.fullmatch(r"risk r[23] requires human approval", reason):
        return (
            ResumeClassification.RISK_APPROVAL,
            "RISK_APPROVAL",
            "The run requires approval under the configured risk policy.",
            "Review the work item risk and approve or change the policy before retrying.",
        )
    if "budget" in reason or "attempt" in reason:
        return (
            ResumeClassification.NOT_RESUMABLE,
            "ATTEMPT_BUDGET_EXHAUSTED",
            "The run exhausted a bounded retry budget.",
            "Inspect the run artifacts, correct the underlying issue, then retry.",
        )
    if "ci " in reason or reason.startswith("ci"):
        return (
            ResumeClassification.NOT_RESUMABLE,
            "CI_INTERVENTION",
            "CI could not be completed or repaired automatically.",
            "Inspect the pull request checks, fix the failing check, then retry delivery.",
        )
    if any(term in reason for term in ("publish", "pull request", "merge", "permission")):
        return (
            ResumeClassification.NOT_RESUMABLE,
            "DELIVERY_INTERVENTION",
            "The controller could not complete pull request delivery.",
            "Check repository permissions and delivery settings, then retry delivery.",
        )
    if any(term in reason for term in ("abandon", "interrupt", "workspace")):
        return (
            ResumeClassification.NOT_RESUMABLE,
            "RECOVERY_INTERVENTION",
            "The run could not safely recover its persisted workspace.",
            "Inspect the run and workspace metadata before starting a replacement run.",
        )
    return (
        ResumeClassification.NOT_RESUMABLE,
        "MANUAL_INSPECTION",
        "The controller stopped at a manual decision boundary.",
        "Inspect the typed run artifacts and decide whether to retry or replace the run.",
    )


def build_escalation_comment(
    *,
    run_id: str,
    episode_id: str,
    classification: ResumeClassification,
    reason_code: str,
    summary: str,
    next_action: str,
    attempts_consumed: int,
    reopen_count: int,
    max_reopens: int,
) -> str:
    """Build concise, safe GitHub comment content.

    Raw failure_reason, workspace paths, issue body/title, command output,
    diffs, logs, and model reasoning are never published.
    """
    marker = format_escalation_marker(run_id, episode_id)
    lines = [
        marker,
        "### Factory Escalation Notice",
        "",
        f"The run `{run_id}` requires human attention.",
        "",
        f"- **Reason code**: `{reason_code}`",
        f"- **Summary**: {summary}",
        f"- **Next action**: {next_action}",
        f"- **Attempts recorded**: {attempts_consumed}",
        f"- **Reopens**: {reopen_count}/{max_reopens}",
        "",
    ]
    if classification is ResumeClassification.RISK_APPROVAL:
        lines.extend(
            [
                "#### Resume instructions",
                "",
                "To approve and resume this run, an authorized human contributor "
                "may reply on this thread with:",
                "",
                "```",
                f"@factory resume v1 run={run_id} episode={episode_id}",
                "```",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "#### Resume instructions",
                "",
                "This halt category cannot be resumed automatically via GitHub reply. "
                "Manual inspection of local artifacts is required.",
                "",
            ]
        )
    return "\n".join(lines)


def resolve_escalation_target(
    run: FactoryRun,
    store: FileRunStore,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    expected_repository: str | None = None,
) -> tuple[RepositoryRef, int, EscalationTargetType, str | None] | None:
    """Resolve the destination for escalation comments: open factory PR first,
    otherwise source issue reference.

    Enforces allowed hosts and binds PR-first targets to authoritative repository
    identities (source issue repository, delivery repository, or service repository).
    Never searches GitHub for arbitrary linked PRs.
    """
    allowed_hosts = {host.casefold() for host in config.escalation.allowed_hosts}

    # Establish authoritative repository identities
    try:
        work_item = store.load_artifact(run.id, WorkItem)
    except (FileNotFoundError, ValueError):
        work_item = None

    source_issue_repo: RepositoryRef | None = None
    issue_number: int | None = None
    hosts = config.escalation.allowed_hosts
    pr_hosts = config.pull_request.allowed_hosts
    default_host = (
        run.delivery_host
        or client.host
        or (pr_hosts[0] if pr_hosts else None)
        or (hosts[0] if hosts else "github.com")
    )
    if work_item and work_item.external_id:
        try:
            source_issue_repo, issue_number = parse_issue_reference(
                work_item.external_id, default_host=default_host
            )
        except ValueError:
            source_issue_repo = None
            issue_number = None

    authoritative_identities: set[tuple[str, str]] = set()
    if source_issue_repo is not None:
        authoritative_identities.add(
            (source_issue_repo.host.casefold(), source_issue_repo.full_name.casefold())
        )
    if run.delivery_repository:
        delivery_repo_host = (
            run.delivery_host
            or (source_issue_repo.host if source_issue_repo else None)
            or default_host
        ).casefold()
        authoritative_identities.add((delivery_repo_host, run.delivery_repository.casefold()))
    if expected_repository:
        if "/" in expected_repository and expected_repository.count("/") >= 2:
            parts = expected_repository.split("/", 1)
            authoritative_identities.add((parts[0].casefold(), parts[1].casefold()))
        else:
            exp_host = (
                run.delivery_host
                or (source_issue_repo.host if source_issue_repo else None)
                or default_host
            ).casefold()
            authoritative_identities.add((exp_host, expected_repository.casefold()))

    # 1. Prefer open persisted factory PR matching authoritative repository
    if run.pull_request_url:
        try:
            pr_repo_ref, pr_number = parse_pull_request_url(run.pull_request_url)
        except ValueError:
            pr_repo_ref, pr_number = None, None

        if pr_repo_ref is not None and pr_number is not None:
            pr_host = pr_repo_ref.host.casefold()
            pr_full_name = pr_repo_ref.full_name.casefold()
            pr_identity = (pr_host, pr_full_name)

            host_is_allowed = pr_host in allowed_hosts
            source_host_agrees = (
                source_issue_repo.host.casefold() == pr_host
                if source_issue_repo is not None
                else True
            )
            delivery_host_agrees = (
                run.delivery_host.casefold() == pr_host if run.delivery_host is not None else True
            )

            matches_authoritative = (
                host_is_allowed
                and source_host_agrees
                and delivery_host_agrees
                and pr_identity in authoritative_identities
            )
            if matches_authoritative:
                try:
                    pr_state = client.get_pull_request(
                        repo_path,
                        str(pr_number),
                        repository=pr_repo_ref.full_name,
                        hostname=pr_repo_ref.host,
                    )
                    if pr_state.state.upper() == "OPEN":
                        return (
                            pr_repo_ref,
                            pr_number,
                            EscalationTargetType.PULL_REQUEST,
                            run.pull_request_url,
                        )
                except GitHubError:
                    logger.debug("could not verify PR state for %s", run.pull_request_url)
            else:
                logger.warning(
                    "ignoring PR url %s: does not match authoritative repo identities %s "
                    "or allowed hosts",
                    run.pull_request_url,
                    authoritative_identities,
                )

    # 2. Fall back to source GitHub issue reference
    if (
        source_issue_repo is not None
        and issue_number is not None
        and source_issue_repo.host.casefold() in allowed_hosts
    ):
        target_url = (
            work_item.external_id
            if work_item and work_item.external_id and work_item.external_id.startswith("http")
            else f"https://{source_issue_repo.host}/{source_issue_repo.full_name}/issues/{issue_number}"
        )
        return source_issue_repo, issue_number, EscalationTargetType.ISSUE, target_url

    return None


def is_authorized_author(
    comment: GitHubComment,
    *,
    authorized_identities: Sequence[str],
    allowed_associations: Sequence[str],
    factory_login: str | None = None,
    factory_id: int | None = None,
) -> bool:
    """Verify that comment author is a human, not a bot, not the factory account,
    matches authorized identities, and possesses an allowed association."""
    # 1. Reject bot
    if comment.user_type.lower() == "bot" or comment.user_login.casefold().endswith("[bot]"):
        return False

    # 2. Reject self even if configured in authorized_identities
    if factory_login and comment.user_login.casefold() == factory_login.casefold():
        return False
    if factory_id is not None and comment.user_id is not None and comment.user_id == factory_id:
        return False

    # 3. Check allowed association
    allowed_set = {assoc.upper() for assoc in allowed_associations}
    if comment.author_association.upper() not in allowed_set:
        return False

    # 4. All-digit entries are immutable user IDs only. Other entries are
    # logins only, so a numeric login cannot impersonate an allowlisted ID.
    authorized_logins = {
        identity.casefold() for identity in authorized_identities if not identity.isdigit()
    }
    authorized_ids = {int(identity) for identity in authorized_identities if identity.isdigit()}
    login_matches = comment.user_login.casefold() in authorized_logins
    id_matches = comment.user_id is not None and comment.user_id in authorized_ids
    return login_matches or id_matches


def deliver_escalation_notification(
    run: FactoryRun,
    store: FileRunStore,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    expected_repository: str | None = None,
) -> FactoryRun:
    """Attempt bounded delivery of an escalation notice comment.

    Errors are persisted on the run's escalation record and never prevent
    the run from remaining in NEEDS_HUMAN.
    """
    if not config.escalation.enabled:
        return run

    if run.state is not WorkflowState.NEEDS_HUMAN:
        return run

    escalation = run.escalation
    if escalation is None:
        classification, code, summary, action = classify_halt_reason(run, store)
        escalation = EscalationRecord(
            episode_id=generate_episode_id(),
            episode_number=1,
            status=EscalationStatus.PENDING_NOTIFICATION,
            resume_classification=classification,
            reason_code=code,
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)

    if escalation.status is EscalationStatus.NOTIFIED:
        return run

    if escalation.delivery_attempts >= config.escalation.max_notification_attempts:
        if escalation.status is not EscalationStatus.NOTIFICATION_FAILED:
            escalation = escalation.model_copy(
                update={"status": EscalationStatus.NOTIFICATION_FAILED}
            )
            run = run.model_copy(update={"escalation": escalation})
            store.save_run(run)
        return run

    attempts = escalation.delivery_attempts + 1
    is_terminal = attempts >= config.escalation.max_notification_attempts

    target = resolve_escalation_target(
        run, store, config, client, repo_path, expected_repository=expected_repository
    )
    if target is None:
        escalation = escalation.model_copy(
            update={
                "delivery_attempts": attempts,
                "delivery_error": "no valid escalation target resolved",
                "status": EscalationStatus.NOTIFICATION_FAILED,
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return run

    repo_ref, target_number, target_type, target_url = target
    classification, code, summary, action = classify_halt_reason(run, store)
    comment_body = build_escalation_comment(
        run_id=run.id,
        episode_id=escalation.episode_id,
        classification=classification,
        reason_code=code,
        summary=summary,
        next_action=action,
        attempts_consumed=len(run.attempt_records),
        reopen_count=escalation.reopen_count,
        max_reopens=config.escalation.max_reopens,
    )

    marker = format_escalation_marker(run.id, escalation.episode_id)
    found_existing = None
    try:
        # Check if already posted before creating a duplicate, bounded up to 3 pages
        since_time = escalation.created_at - timedelta(minutes=2)
        for page_idx in range(1, 4):
            existing_comments = client.list_issue_comments(
                repo_path,
                repository=repo_ref.full_name,
                issue_number=target_number,
                since=since_time,
                page=page_idx,
                per_page=100,
                hostname=repo_ref.host,
            )
            for existing in existing_comments:
                if marker in existing.body:
                    found_existing = existing
                    break
            if found_existing is not None or len(existing_comments) < 100:
                break
    except GitHubError as exc:
        logger.debug("could not check existing comments for run %s: %s", run.id, exc)

    reply_cursor = "closed" if classification is not ResumeClassification.RISK_APPROVAL else None

    if found_existing is not None:
        escalation = escalation.model_copy(
            update={
                "target_type": target_type,
                "target_host": repo_ref.host,
                "target_repository": repo_ref.full_name,
                "target_number": target_number,
                "target_url": target_url,
                "delivery_attempts": attempts,
                "comment_id": found_existing.id,
                "comment_url": found_existing.html_url or found_existing.url,
                "status": EscalationStatus.NOTIFIED,
                "delivery_error": None,
                "last_notified_at": utc_now(),
                "reply_cursor": reply_cursor,
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return run

    try:
        posted = client.create_issue_comment(
            repo_path,
            repository=repo_ref.full_name,
            issue_number=target_number,
            body=comment_body,
            hostname=repo_ref.host,
        )
        escalation = escalation.model_copy(
            update={
                "target_type": target_type,
                "target_host": repo_ref.host,
                "target_repository": repo_ref.full_name,
                "target_number": target_number,
                "target_url": target_url,
                "delivery_attempts": attempts,
                "comment_id": posted.id,
                "comment_url": posted.html_url or posted.url,
                "status": EscalationStatus.NOTIFIED,
                "delivery_error": None,
                "last_notified_at": utc_now(),
                "reply_cursor": reply_cursor,
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
    except GitHubError as exc:
        escalation = escalation.model_copy(
            update={
                "target_type": target_type,
                "target_host": repo_ref.host,
                "target_repository": repo_ref.full_name,
                "target_number": target_number,
                "target_url": target_url,
                "delivery_attempts": attempts,
                "delivery_error": f"notification delivery error: {exc}",
                "status": (
                    EscalationStatus.NOTIFICATION_FAILED
                    if is_terminal
                    else EscalationStatus.PENDING_NOTIFICATION
                ),
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
    return run


def validate_reply_candidate(
    comment: GitHubComment,
    *,
    run: FactoryRun,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    factory_login: str | None = None,
    factory_id: int | None = None,
    now: datetime | None = None,
) -> ValidationResult:
    """Validate a candidate comment against all security and protocol rules.

    Returns ValidationResult(is_valid, reason, retryable=...).
    """
    escalation = run.escalation
    if escalation is None or escalation.status is not EscalationStatus.NOTIFIED:
        return ValidationResult(False, "run does not have an active notified escalation")

    if escalation.target_repository is None or escalation.target_number is None:
        return ValidationResult(False, "run escalation target is incomplete")

    # Check command grammar
    parsed = parse_resume_command(comment.body)
    if parsed is None:
        return ValidationResult(False, "command does not match exact resume grammar")

    cmd_run, cmd_episode = parsed
    if cmd_run != run.id:
        return ValidationResult(False, f"command run id {cmd_run!r} does not match {run.id!r}")

    if cmd_episode != escalation.episode_id:
        return ValidationResult(
            False, f"command episode id {cmd_episode!r} does not match {escalation.episode_id!r}"
        )

    # Timing: must be created after escalation
    ref_time = escalation.created_at
    if comment.created_at < ref_time:
        return ValidationResult(False, "comment was created before escalation episode")

    # Window check
    current_time = now or utc_now()
    window_deadline = escalation.created_at + timedelta(hours=config.escalation.reply_window_hours)
    if current_time > window_deadline:
        return ValidationResult(False, "reply window has expired")

    # Reopen limits
    if escalation.reopen_count >= config.escalation.max_reopens:
        return ValidationResult(
            False,
            f"reopen limit reached ({escalation.reopen_count}/{config.escalation.max_reopens})",
        )

    # Resumable class check
    if escalation.resume_classification is not ResumeClassification.RISK_APPROVAL:
        return ValidationResult(
            False, f"halt category {escalation.resume_classification} is not resumable via reply"
        )

    # Replay check
    if any(receipt.comment_id == comment.id for receipt in escalation.accepted_replies):
        return ValidationResult(False, f"comment {comment.id} has already been accepted")

    # Target host check
    target_host = escalation.target_host or (
        config.escalation.allowed_hosts[0] if config.escalation.allowed_hosts else "github.com"
    )
    allowed_hosts = {h.casefold() for h in config.escalation.allowed_hosts}
    if target_host.casefold() not in allowed_hosts:
        return ValidationResult(False, f"target host {target_host!r} is not allowed")

    # Author authorization (must verify authenticated factory identity to fail closed)
    if factory_login is None and factory_id is None:
        try:
            identity = client.get_authenticated_user(repo_path, hostname=target_host)
            factory_login = identity.login
            factory_id = identity.id
        except GitHubError as exc:
            return ValidationResult(
                False, f"cannot prove author is not factory account: {exc}", retryable=True
            )
        if not factory_login and factory_id is None:
            return ValidationResult(
                False,
                "cannot prove author is not factory account: identity unresolved",
                retryable=True,
            )

    if not is_authorized_author(
        comment,
        authorized_identities=config.escalation.authorized_identities,
        allowed_associations=config.escalation.allowed_associations,
        factory_login=factory_login,
        factory_id=factory_id,
    ):
        return ValidationResult(False, "author is not authorized")

    # Immediate re-fetch on exact host to detect edits or deletions

    try:
        fresh = client.get_issue_comment(
            repo_path,
            repository=escalation.target_repository,
            comment_id=comment.id,
            hostname=target_host,
        )
    except GitHubError as exc:
        return ValidationResult(
            False, f"could not re-fetch comment {comment.id}: {exc}", retryable=True
        )

    if fresh.id != comment.id:
        return ValidationResult(False, "re-fetched comment id mismatch")

    if fresh.created_at != comment.created_at:
        return ValidationResult(False, "re-fetched comment creation time mismatch")

    if fresh.updated_at != fresh.created_at:
        return ValidationResult(False, "comment was edited after creation")

    fresh_parsed = parse_resume_command(fresh.body)
    if fresh_parsed != parsed:
        return ValidationResult(False, "re-fetched comment body no longer matches resume command")

    if not is_authorized_author(
        fresh,
        authorized_identities=config.escalation.authorized_identities,
        allowed_associations=config.escalation.allowed_associations,
        factory_login=factory_login,
        factory_id=factory_id,
    ):
        return ValidationResult(False, "re-fetched author is not authorized")

    return ValidationResult(True, "valid")


def poll_escalation_reply(
    run: FactoryRun,
    store: FileRunStore,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    *,
    factory_login: str | None = None,
    factory_id: int | None = None,
    now: datetime | None = None,
) -> AcceptedReplyReceipt | None:
    """Poll GitHub comments for an authorized reply to an escalated run.

    Uses bounded pagination across ticks with a persisted cursor to avoid
    missing comments and avoid unbounded scans. If a valid reply is found,
    records the decision receipt on the run and persists it to disk before returning.
    """
    if not config.escalation.enabled:
        return None

    if run.state is not WorkflowState.NEEDS_HUMAN:
        return None

    escalation = run.escalation
    if escalation is None or escalation.status is not EscalationStatus.NOTIFIED:
        return None

    if escalation.reply_cursor == "closed":
        return None

    target_repo = escalation.target_repository
    target_num = escalation.target_number
    if target_repo is None or target_num is None:
        return None

    current_time = now or utc_now()
    if escalation.resume_classification is not ResumeClassification.RISK_APPROVAL:
        escalation = escalation.model_copy(
            update={"reply_cursor": "closed", "updated_at": current_time}
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return None

    window_deadline = escalation.created_at + timedelta(hours=config.escalation.reply_window_hours)
    if current_time > window_deadline:
        escalation = escalation.model_copy(
            update={
                "status": EscalationStatus.EXPIRED,
                "reply_cursor": "closed",
                "updated_at": current_time,
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return None

    if escalation.reopen_count >= config.escalation.max_reopens:
        escalation = escalation.model_copy(
            update={"reply_cursor": "closed", "updated_at": current_time}
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return None

    target_host = escalation.target_host or (
        config.escalation.allowed_hosts[0] if config.escalation.allowed_hosts else "github.com"
    )
    allowed_hosts = {h.casefold() for h in config.escalation.allowed_hosts}
    if target_host.casefold() not in allowed_hosts:
        return None

    if factory_login is None and factory_id is None:
        try:
            identity = client.get_authenticated_user(repo_path, hostname=target_host)
            factory_login = identity.login
            factory_id = identity.id
        except GitHubError as exc:
            logger.warning(
                "could not resolve authenticated github user on %s; skipping reply polling: %s",
                target_host,
                exc,
            )
            return None
        if not factory_login and factory_id is None:
            logger.warning(
                "authenticated github user unverified on %s; skipping reply polling",
                target_host,
            )
            return None

    cursor_page = 1
    cursor_since = escalation.created_at
    cursor_last_id: int | None = None
    if escalation.reply_cursor and escalation.reply_cursor != "closed":
        try:
            cursor_data = json.loads(escalation.reply_cursor)
            if isinstance(cursor_data, dict):
                cursor_page = max(1, int(cursor_data.get("page", 1)))
                if "since" in cursor_data and cursor_data["since"]:
                    cursor_since = datetime.fromisoformat(cursor_data["since"])
                if "last_id" in cursor_data and cursor_data["last_id"] is not None:
                    cursor_last_id = int(cursor_data["last_id"])
        except (ValueError, TypeError, json.JSONDecodeError):
            cursor_page = 1
            cursor_since = escalation.created_at
            cursor_last_id = None

    max_pages_per_poll = 2
    current_page = cursor_page
    next_cursor = None
    accepted_receipt = None
    latest_id = cursor_last_id
    latest_timestamp = cursor_since

    for _ in range(max_pages_per_poll):
        try:
            comments = client.list_issue_comments(
                repo_path,
                repository=target_repo,
                issue_number=target_num,
                since=cursor_since,
                page=current_page,
                per_page=100,
                hostname=target_host,
            )
        except GitHubError as exc:
            logger.debug("error listing comments for run %s: %s", run.id, exc)
            break

        if not comments:
            next_cursor = json.dumps(
                {
                    "page": 1,
                    "since": latest_timestamp.isoformat(),
                    "last_id": latest_id,
                }
            )
            break

        retryable_stopped = False
        for comment in comments:
            if (
                cursor_last_id is not None
                and comment.created_at == cursor_since
                and comment.id <= cursor_last_id
            ):
                continue

            result = validate_reply_candidate(
                comment,
                run=run,
                config=config,
                client=client,
                repo_path=repo_path,
                factory_login=factory_login,
                factory_id=factory_id,
                now=current_time,
            )
            if result.is_valid:
                accepted_receipt = AcceptedReplyReceipt(
                    comment_id=comment.id,
                    user_login=comment.user_login,
                    user_id=comment.user_id,
                    author_association=comment.author_association,
                    created_at=comment.created_at,
                    accepted_at=current_time,
                    command=f"@factory resume v1 run={run.id} episode={escalation.episode_id}",
                    episode_id=escalation.episode_id,
                    run_id=run.id,
                )
                break

            if getattr(result, "retryable", False):
                logger.info(
                    "retryable validation failure for comment %s on run %s: %s; "
                    "stopping cursor advancement",
                    comment.id,
                    run.id,
                    result.reason,
                )
                retryable_stopped = True
                break

            if latest_id is None or comment.id > latest_id:
                latest_id = comment.id
            if comment.created_at > latest_timestamp:
                latest_timestamp = comment.created_at

        if accepted_receipt is not None or retryable_stopped:
            if retryable_stopped and latest_timestamp is not None:
                next_cursor = json.dumps(
                    {
                        "page": 1,
                        "since": latest_timestamp.isoformat(),
                        "last_id": latest_id,
                    }
                )
            break

        if len(comments) < 100:
            next_cursor = json.dumps(
                {
                    "page": 1,
                    "since": latest_timestamp.isoformat(),
                    "last_id": latest_id,
                }
            )
            break
        else:
            current_page += 1
            next_cursor = json.dumps(
                {
                    "page": current_page,
                    "since": cursor_since.isoformat(),
                    "last_id": latest_id,
                }
            )

    if accepted_receipt is not None:
        escalation = escalation.model_copy(
            update={
                "accepted_replies": [*escalation.accepted_replies, accepted_receipt],
                "reopen_count": escalation.reopen_count + 1,
                "status": EscalationStatus.REOPENED,
                "reply_cursor": "closed",
                "updated_at": current_time,
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return accepted_receipt

    if next_cursor is not None and next_cursor != escalation.reply_cursor:
        escalation = escalation.model_copy(
            update={"reply_cursor": next_cursor, "updated_at": current_time}
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)

    return None


def reconcile_undelivered_notifications(
    store: FileRunStore,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    *,
    max_runs: int = 10,
    expected_repository: str | None = None,
) -> list[FactoryRun]:
    """Find and deliver notifications for runs in NEEDS_HUMAN needing delivery."""
    if not config.escalation.enabled:
        return []

    updated_runs: list[FactoryRun] = []
    runs = store.list_runs()
    for run in runs:
        if len(updated_runs) >= max_runs:
            break
        if run.state is not WorkflowState.NEEDS_HUMAN:
            continue
        escalation = run.escalation
        needs_notification = escalation is None or (
            escalation.status
            in {
                EscalationStatus.PENDING_NOTIFICATION,
                EscalationStatus.NOTIFICATION_FAILED,
            }
            and escalation.delivery_attempts < config.escalation.max_notification_attempts
        )
        if needs_notification:
            updated = deliver_escalation_notification(
                run,
                store,
                config,
                client,
                repo_path,
                expected_repository=expected_repository,
            )
            updated_runs.append(updated)

    return updated_runs
