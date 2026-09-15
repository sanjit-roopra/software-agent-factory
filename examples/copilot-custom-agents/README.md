# Copilot CLI-style subagent fleet for VS Code

This directory models the seven Copilot CLI subagent roles in VS Code.

Copy `AGENTS.md` and `.github` into the root of your repository.

```text
your-repository/
├── AGENTS.md
└── .github/
    ├── copilot-instructions.md
    └── agents/
        ├── subagent-fleet.agent.md
        ├── explore.agent.md
        ├── task.agent.md
        ├── general-purpose.agent.md
        ├── rubber-duck.agent.md
        ├── code-review.agent.md
        ├── research.agent.md
        └── security-review.agent.md
```

Customize `AGENTS.md` and `copilot-instructions.md` for your repository.

## Start the fleet in VS Code

Install GitHub Copilot and sign in.

Open the repository in VS Code.

Run **Chat: Open Customizations** from the Command Palette.

Confirm that **Subagent Fleet** appears in the custom agent list.

Select **Subagent Fleet** in the Chat view.

Give it a focused request.

The coordinator can delegate only to the seven hidden specialists.

Each specialist has the model selected in the supplied CLI screenshot.

VS Code uses a model only when your account and organization permit it.

The main session model must meet each subagent's cost tier.

When a named model is unavailable, change or remove that profile's `model` value.

## Roles and configured models

| Role | Model | Purpose |
| --- | --- | --- |
| Explore | Gemini 3.8 Flash | Fast read-only investigation |
| Task | Gemini 3.8 Flash | Run one development command |
| General Purpose | Gemini 3.8 Flash | Complex multi-step work |
| Rubber Duck | Claude Opus 5 | Independent constructive critique |
| Code Review | GPT-5.6 Sol | High-confidence code review |
| Research | GPT-5.6 Terra | Cited explicit research |
| Security Review | GPT-6 Astra | Exploitable vulnerability review |

## Important difference from Copilot CLI

These are VS Code custom-agent profiles.

They apply documented role responsibilities and your model choices.

They do not copy the private Copilot CLI implementation or its internal prompts.

GitHub publishes behavior and broad permissions for all seven roles.

The Copilot CLI license does not allow modified derivative copies.

VS Code custom agents also cannot force delegation, retries, ordering, or fleet concurrency.

The model decides when to invoke an allowed subagent.

Subagents return summaries and do not retain a writable conversation.

The native CLI selects Rubber Duck from a complementary model family.

This sample fixes Rubber Duck to Claude Opus 5, as in your CLI settings.

## Copilot CLI

Your existing CLI configuration already controls the native built-in roles.

Use `/subagents` to select models for those roles.

Use `/fleet` for parallel subagent execution.

Use `/tasks` to view or stop subagent work.

Do not copy these VS Code profiles into the CLI as replacements.

They use `target: vscode` to avoid shadowing the native CLI agents.

## Sources

- [Copilot CLI built-in agents](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/about-custom-agents)
- [Copilot CLI model settings](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference#configuration-file-settings)
- [VS Code custom agents](https://code.visualstudio.com/docs/agent-customization/custom-agents)
- [VS Code subagents](https://code.visualstudio.com/docs/agents/run/subagents)
- [Custom agent tool aliases](https://docs.github.com/en/copilot/reference/custom-agents-configuration#tool-aliases)
