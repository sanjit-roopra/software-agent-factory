# Share a subagent fleet with your team

A custom agent is a Markdown file that gives Copilot a role, a prompt, a model,
and a tool list. A subagent is a custom agent that another agent calls to do one
part of a task.

This guide explains the agent files in this directory. The fleet has one
coordinator and seven specialists. It is a standalone set of GitHub Copilot
custom agents, and it does not depend on the rest of this repository.

The same files work in VS Code, on GitHub.com, and in JetBrains IDEs.

## Why a fleet helps

A single chat holds one context window. Long tasks fill it with search output,
test logs, and dead ends.

A subagent runs in its own context window and returns only its final result. The
main chat therefore keeps the plan and the decisions.

Each specialist also uses its own model. A cheap model reads code, and an
expensive model reviews the result.

## Install the fleet

Copy `AGENTS.md` and `.github` from this directory into the root of your
repository:

```bash
cp -R AGENTS.md ~/your-repository/
cp -R .github ~/your-repository/
```

Edit `.github/copilot-instructions.md` for your language, your package tool, and
your test command.

Commit the files. GitHub.com reads agent profiles from the branch, so an
uncommitted file does not work there.

Restart your editor to load the new profiles.

## The roles

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

Only `Fleet General Purpose` can edit files. `Fleet Task` runs commands, and the
review roles run read-only commands such as `git diff`. The other specialists
read and search only.

## Start the fleet in VS Code

Open the repository in VS Code.

Open the Chat view.

Select **Subagent Fleet** in the agent dropdown.

Send one focused request.

To confirm that the profiles loaded, run **Chat: Open Customizations** from the
Command Palette.

## Start a single specialist

Select the specialist in the agent dropdown, then send the request.

Use a single specialist for a small task, because the coordinator adds one extra
model call.

Direct selection is also the reliable path on a surface that does not delegate
to subagents.

## Write a good fleet request

A subagent does not see your chat history. It receives the task prompt, the
instruction files, and its own configuration.

State the file paths, the command, and the acceptance criteria in the request.

Good request:

```text
The retry helper in src/client/retry.py drops the last error.
Find the cause, fix it, add a unit test, then run pytest tests/client.
```

Poor request:

```text
Fix that bug we discussed.
```

## Example session

```text
You:   Add a --dry-run flag to the export command. Review the result.

Fleet: Fleet Explore    -> finds the command in src/cli/export.py:41
       Fleet General Purpose -> adds the flag, the branch, and two tests
       Fleet Task       -> runs pytest tests/cli, reports 34 passed
       Fleet Code Review -> reports one unhandled None on line 58
       Fleet General Purpose -> fixes line 58
       Fleet Task       -> runs pytest tests/cli again, reports 35 passed
```

The coordinator reports the result of each step and names the specialist.

## Limits you must know

Read these limits before you file a bug against the fleet.

The subagent model cannot exceed the cost tier of the main session model. A more
expensive subagent does not run, and it reports the available models instead.
Select a strong model in the picker before you start the fleet.

A model name must be available to your account and your organization. When a
name is not available, edit the `model` line or delete it. An agent without a
`model` line uses the model in the picker.

Each subagent call is stateless. The coordinator cannot send a follow-up message
to one subagent. It starts a new call instead.

The main model decides when to call a specialist. A profile cannot force
delegation, retries, or a fixed order. Name the specialist in your request for
tighter control.

The `agents` allowlist in the coordinator is a VS Code property. GitHub.com
ignores it, so the coordinator there can call any custom agent in the
repository.

Agent names are case-sensitive. An edited `name` value must also change in the
coordinator allowlist.

## Adapt the fleet for your team

Change a model: edit the `model` line in the specialist file.

Change behavior: edit the Markdown body below the frontmatter. The body is the
prompt, and it has a limit of 30,000 characters.

Add a role: copy a specialist file, give it a new `name`, then add that exact
name to the `agents` list in `subagent-fleet.agent.md`.

Restrict a role: list fewer tool aliases. The aliases are `read`, `search`,
`edit`, `execute`, `web`, `agent`, and `todo`. An absent `tools` property
enables all tools.

## Copilot CLI

Copilot CLI ships equivalent built-in subagents. Do not install this fleet to
replace them.

Use `/subagents` in the CLI to pick the model for each built-in role.

The example file names use a `fleet-` prefix. The prefix keeps the profiles
separate from the built-in CLI roles, which use the names `explore`, `task`,
`general-purpose`, `code-review`, `research`, `rubber-duck`, and
`security-review`.

## Sources

- [Custom agents configuration](https://docs.github.com/en/copilot/reference/custom-agents-configuration)
- [Copilot CLI built-in agents](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/about-custom-agents)
- [Custom agents in VS Code](https://code.visualstudio.com/docs/agent-customization/custom-agents)
- [Subagents in VS Code](https://code.visualstudio.com/docs/agents/run/subagents)
