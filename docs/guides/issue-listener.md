# Run a GitHub issue listener

This guide starts one local listener for GitHub Issues.

The listener processes open issues with the `agent-ready` label. It creates a
Git worktree for each accepted issue. It can create a draft pull request.

## 1. Configure the repository checks

First, follow [Configure a repository](configure-repository.md). Set the
`install`, `verify`, and `build` commands for the target repository.

## 2. Copy the example configuration

Keep the configuration outside the target repository.

```bash
mkdir -p ~/.config/software-agent-factory/repositories
cp config/factory.example.yaml \
  ~/.config/software-agent-factory/repositories/example-listener.yaml
```

Set a stable `factory.data_dir`. The data directory stores run evidence,
worktrees, logs, and reusable repository guidance.

```yaml
factory:
  data_dir: "~/.software-factory"
```

## 3. Configure the listener

Use one worker and a 60-second poll interval.

```yaml
pull_request:
  enabled: true
  remote: "origin"
  base_branch: "main"
  draft: true
  allowed_hosts: ["github.com"]

ci:
  enabled: false

merge:
  enabled: false

scheduler:
  enabled: true
  poll_interval_seconds: 60
  max_concurrent_tasks: 1
  stall_timeout_seconds: 3600
  required_label: "agent-ready"
  max_runs_per_day: 20
```

This configuration creates draft pull requests. It does not monitor CI or
merge changes.

## 4. Configure human approval replies

When a human must approve risk-sensitive work, enable escalation.

```yaml
escalation:
  enabled: true
  authorized_identities:
    - "YOUR-GITHUB-LOGIN"
  allowed_associations:
    - "OWNER"
    - "MEMBER"
    - "COLLABORATOR"

risk:
  R0: { human_approval: false }
  R1: { human_approval: false }
  R2: { human_approval: true }
  R3: { human_approval: true }
```

`authorized_identities` controls escalation replies only. The scheduler accepts
each open issue with `agent-ready`. It does not filter issue authors.

Control access to the `agent-ready` label in GitHub. Do not start a paid
listener until the label policy meets your requirements.

## 5. Create the label and issue

When it does not exist, create the label once.

```bash
gh label create agent-ready \
  --repo OWNER/REPOSITORY \
  --color 0E8A16 \
  --description "Approved for Software Agent Factory"
```

Create an issue with a clear outcome and acceptance criteria.

```bash
gh issue create \
  --repo OWNER/REPOSITORY \
  --label agent-ready \
  --title "Reject invalid customer input" \
  --body $'Outcome:\nReject invalid input.\n\nAcceptance criteria:\n- Invalid input returns a clear error.\n- Existing tests pass.'
```

The listener ignores pull requests. It processes only open, labeled issues.

## 6. Check prerequisites

Run this command before you start the listener.

```bash
factory doctor \
  --config ~/.config/software-agent-factory/repositories/example-listener.yaml \
  --runtime copilot \
  --model-profile economy
```

The `copilot` runtime makes paid model calls. The `economy` profile uses the
lower-cost configured model route.

## 7. Start the listener

Run this command from the factory source checkout.

```bash
uv run factory start \
  --repo ~/projects/example \
  --github-repo OWNER/REPOSITORY \
  --config ~/.config/software-agent-factory/repositories/example-listener.yaml \
  --runtime copilot \
  --model-profile economy
```

Press Ctrl-C to stop after the current scheduler cycle.

Use `--once` to run one scheduler cycle instead of continuous polling.

## 8. Reply to a human request

Copy the run and episode values from the factory notice. Reply on the same
GitHub thread.

```text
@factory resume v1 run=<run-id> episode=<episode-id>
```

Only an authorized user can send this approval reply. It resumes only
`RISK_APPROVAL` requests.

For a `PLAN_DECISION` request, copy the header from the notice. Answer every
numbered decision in order.

```text
@factory answer v1 run=<run-id> episode=<episode-id>
1. First decision answer.
2. Second decision answer.
```

The controller validates and saves the answers. It returns the same run to
planning. It checks the new plan before implementation starts.

## 9. Inspect the listener

Use the status command to inspect persisted runs.

```bash
factory status
```

For a persistent macOS process, read [Monitor and run continuously](operations.md).
