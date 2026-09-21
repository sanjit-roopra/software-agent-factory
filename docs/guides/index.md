# Guides

Task guides for common workflows. Read [Get started](../get-started/index.md) first.

[Configure a repository](configure-repository.md)
: Configure the real install, verify, and build commands for your project.
  Understand deterministic gates and scope-drift checks.

[Repository skills and overlays](repository-skills.md)
: Understand reusable generated guidance. Edit your own
  `repository-skill-overlay.yaml` file outside the target repository.

[Adaptive execution routing](adaptive-routing.md)
: Configure Jev to select fast execution routes for eligible work items.
  Understand routes, safety floors, and post-implementation ratchets.

[GitHub backlog, PRs and CI](github.md)
: Poll `agent-ready` issues, open draft pull requests, watch CI, and repair failures.

[Run a GitHub issue listener](issue-listener.md)
: Configure, label, start, approve, and inspect one local issue listener.

[Monitor and run continuously](operations.md)
: Use `factory status`, structured logs, the read-only local dashboard, and the
  macOS launchd service.

[Troubleshooting](troubleshooting.md)
: Exit codes, refusals, escalations, and common causes.
