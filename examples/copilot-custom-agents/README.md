# Copilot custom-agent example

Copy the hidden `.github` directory and `AGENTS.md` into the root of a repository.

Customize `copilot-instructions.md` before you use the sample.

```text
your-repository/
├── AGENTS.md
└── .github/
    ├── copilot-instructions.md
    └── agents/
        ├── feature-builder.agent.md
        ├── implementer.agent.md
        ├── planner.agent.md
        └── reviewer.agent.md
```

`copilot-instructions.md` gives guidance to Copilot requests.

`AGENTS.md` gives guidance to agent sessions.

The `.agent.md` files define named custom agents.

## VS Code

Install GitHub Copilot and GitHub Copilot Chat.

Open the repository folder in VS Code.

Open **Chat: Open Customizations** from the Command Palette.

Confirm that the four agents appear in the agent list.

Select **Feature Builder** in the Chat view.

Give it a focused request.

The coordinator asks the Planner, Implementer, and Reviewer to work as subagents.

The `agent` tool and `agents` list enable this delegation.

The specialist agents stay hidden from the picker.

They can run only as subagents of Feature Builder.

Each subagent runs in isolated context and returns a summary to the coordinator.

## GitHub Copilot CLI

Start Copilot from the repository root.

```bash
copilot
```

Use `/agent` to select a discovered custom agent.

Use `/subagents` to configure the built-in subagent types.

Use `/fleet` to run suitable work in parallel.

The CLI built-in subagents and repository custom agents solve different problems.

Built-in subagents provide delegated roles such as `explore` and `code-review`.

Custom agents provide reusable repository-specific instructions and tool limits.

## Other Copilot clients

The shared instruction files work across Copilot clients.

VS Code, Copilot cloud agent, and Copilot CLI support `.github/agents/*.agent.md` profiles.

JetBrains IDEs, Eclipse, and Xcode support custom agents in public preview.

For Visual Studio, use repository instructions and prompt files.

Before you rely on custom agents, verify support in your installed client.

## Customize the sample

Change each agent description to match how your team works.

Grant only the tools that the role needs.

Keep planning and review agents read-only.

Allow editing and commands only for implementation agents.

Do not place credentials or production commands in agent files.
