# Subagent fleet for Copilot custom agents

This directory contains a coordinator agent and seven specialist agents.

The specialists copy the role split that Copilot CLI uses for its built-in
subagents. The files work in VS Code, on GitHub.com, and in JetBrains IDEs.

This directory is standalone. It contains only GitHub Copilot custom agents, and
it does not depend on the rest of this repository.

Read [USAGE.md](USAGE.md) for the guide you can share with engineers.

## Install

Copy `AGENTS.md` and `.github` into the root of your repository.

```text
your-repository/
├── AGENTS.md
└── .github/
    ├── copilot-instructions.md
    └── agents/
        ├── subagent-fleet.agent.md
        ├── fleet-explore.agent.md
        ├── fleet-task.agent.md
        ├── fleet-general-purpose.agent.md
        ├── fleet-rubber-duck.agent.md
        ├── fleet-code-review.agent.md
        ├── fleet-research.agent.md
        └── fleet-security-review.agent.md
```

Edit `AGENTS.md` and `copilot-instructions.md` for your repository.

Commit the files, because GitHub.com reads agent profiles from the branch.

## Roles and models

| Agent name | Model | Use it for |
| --- | --- | --- |
| Subagent Fleet | picker model | Coordination and delegation |
| Fleet Explore | Gemini 3.8 Flash | Fast read-only investigation |
| Fleet Task | Gemini 3.8 Flash | One command and its output |
| Fleet General Purpose | Gemini 3.8 Flash | Complex multi-step work |
| Fleet Rubber Duck | Claude Opus 5 | Independent second opinion |
| Fleet Code Review | GPT-5.6 Sol | High-confidence defect review |
| Fleet Research | GPT-5.6 Terra | Cited research |
| Fleet Security Review | GPT-6 Astra | Exploitable vulnerability review |

The coordinator sets no model, so it uses the model in the chat model picker.

## Use the fleet in VS Code

Install GitHub Copilot, then sign in.

Open the repository in VS Code.

Select **Subagent Fleet** in the agent dropdown in the Chat view.

Send a focused request.

VS Code runs each specialist in a separate context window, and the specialist
returns only its final result.

The coordinator can start independent specialists in parallel.

## Use the fleet on GitHub.com and in JetBrains IDEs

No file sets the `target` property, so each agent is available in both the
VS Code environment and the GitHub.com environment.

To assign work to Copilot, select the agent by name.

Custom agents are in public preview for JetBrains IDEs, Eclipse, and Xcode.

When a surface does not delegate to subagents, select a specialist directly.

## Limits you must know

The subagent model cannot exceed the cost tier of the main session model. A more
expensive subagent does not run, and it reports the available models instead.

Select a strong model in the picker before you start the fleet.

A model name must be available to your account and your organization. When a
name is not available to you, edit or remove the `model` line.

Each subagent call is stateless, and the coordinator cannot send a follow-up
message to the same subagent.

The `agents` allowlist is a VS Code property. GitHub.com ignores it, so the
coordinator there can call any custom agent that the repository defines.

A subagent does not see the main conversation. It receives the task prompt, the
instruction files, and its own configuration.

## Design decisions

The file names use a `fleet-` prefix. The prefix keeps these profiles separate
from the built-in Copilot CLI agents, which use the names `explore`, `task`,
`general-purpose`, `code-review`, `research`, `rubber-duck`, and
`security-review`.

No specialist sets `disable-model-invocation`. In VS Code that property blocks
subagent use, and only an explicit `agents` entry overrides it. GitHub.com has
no `agents` allowlist, so the property can hide the specialist there.

No specialist sets `user-invocable: false`. You can therefore select a
specialist directly, which matters on surfaces that do not delegate.

`Fleet General Purpose` sets no `tools` property, because an absent `tools`
property enables all tools. Every other role lists the tools that it needs.

`Fleet Code Review` and `Fleet Security Review` include the `execute` alias,
because a diff review needs `git diff`. Their prompts allow read-only commands
only.

## Copilot CLI

Copilot CLI already ships equivalent built-in agents.

Use `/subagents` to pick the model for each built-in role.

Do not expect these files to replace the built-in CLI roles.

## Differences from the built-in Copilot CLI agents

These profiles apply the published role responsibilities. They do not copy the
private Copilot CLI prompts, because the Copilot CLI license does not allow
modified derivative copies.

The built-in CLI Rubber Duck runs on a model family that differs from the
session model. This example fixes the model instead.

The built-in CLI Explore role can also run shell commands. `Fleet Explore` reads
and searches only.

A custom agent profile cannot force delegation, retries, ordering, or
concurrency. The main model decides when to call an allowed subagent.

## Sources

- [Custom agents configuration](https://docs.github.com/en/copilot/reference/custom-agents-configuration)
- [Copilot CLI built-in agents](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/about-custom-agents)
- [Custom agents in VS Code](https://code.visualstudio.com/docs/agent-customization/custom-agents)
- [Subagents in VS Code](https://code.visualstudio.com/docs/agents/run/subagents)
