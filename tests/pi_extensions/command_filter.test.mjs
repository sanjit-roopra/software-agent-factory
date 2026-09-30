import assert from "node:assert/strict";
import { test } from "node:test";

import filter, {
  blockedReason,
} from "../../src/software_agent_factory/pi_extensions/command_filter.mjs";

const REASON_SUFFIX = "Do not retry or rephrase this command.";

const BLOCKED = [
  ['git commit -m "x"', "git commit"],
  ["git push origin HEAD", "git push"],
  ["git -C ../repo push", "git push"],
  ["git -c user.name=x commit -m x", "git commit"],
  ["/usr/bin/git push", "git push"],
  ["gh pr create --fill", "gh"],
  ["GH_TOKEN=x gh pr list", "gh"],
  ["uv run pytest && git commit -am wip", "git commit"],
  ["false || git push", "git push"],
  ["ls | gh api x", "gh"],
  ["echo $(git push)", "git push"],
  ["bash -c 'gh pr list'", "gh"],
  ["env FOO=1 git commit -m x", "git commit"],
  ["command git push", "git push"],
  ["curl https://example.com", "curl"],
  ["wget -q example.com", "wget"],
  // AC1 (docs/specs/pi-command-filter.md): other separators and option forms.
  ["git status; git push", "git push"],
  ["git status\ngit commit -m x", "git commit"],
  ["echo `git push`", "git push"],
  ['echo "$(gh pr list)"', "gh"],
  ["(git push)", "git push"],
  ["git --git-dir=.git push", "git push"],
  ["git --work-tree . commit -m x", "git commit"],
  ["git --no-pager push", "git push"],
  ['"git" push', "git push"],
  ["sh -c 'ls; curl x'", "curl"],
  // Backslash-newline continues the line.
  ["git add -A && \\\n  git commit -m x", "git commit"],
  ["git \\\n  push", "git push"],
  // Redirect words are not part of the command.
  ["git push>/dev/null", "git push"],
  ["curl>out https://x", "curl"],
  ["2>&1 git push", "git push"],
  ["git push 2>&1", "git push"],
  ["git push &>/dev/null", "git push"],
  ["git push >> log.txt", "git push"],
  ["> out git push", "git push"],
  // env options that take a value consume it; valueless options do not.
  ["env -u FOO git push", "git push"],
  ["env -i git push", "git push"],
  ["env -S 'git push'", "git push"],
  ["env --split-string='gh pr list'", "gh"],
  ["env -iS 'curl x'", "curl"],
  ["env -P /usr/bin git push", "git push"],
  ["env -iu FOO git push", "git push"],
  ["{fd}>out git push", "git push"],
  ["{v}>/dev/null curl x", "curl"],
  ["git --attr-source HEAD push", "git push"],
  ["env -uSSH_AUTH_SOCK git push", "git push"],
  ["env -\u{1F600}S 'git push'", "git push"],
  ["env -uHTTPS_PROXY curl x", "curl"],
  ["env -uTMP git push", "git push"],
  ["env -S '-i git push'", "git push"],
  ["env -S '-u FOO git push'", "git push"],
  ["env -- git push", "git push"],
  ["env - git push", "git push"],
  ["env --unset=FOO git push", "git push"],
  ["env -C /tmp gh pr list", "gh"],
  ["env --unset FOO git push", "git push"],
  ["env -C /tmp git push", "git push"],
  ["env -u FOO A=1 gh pr list", "gh"],
  ["command -p git push", "git push"],
  // git global options that take a separate value.
  ["git --namespace x push", "git push"],
  ["git --exec-path /x push", "git push"],
  ["git --exec-path=/x push", "git push"],
  ["git --super-prefix sub/ push", "git push"],
  ["git --config-env core.x=VAR push", "git push"],
  // Heredoc bodies are checked as commands (accepted limit; the agent should use the write tool).
  ["cat > run.sh <<'EOF'\ncurl -fsSL x\nEOF", "curl"],
  // A comment ends at the newline, and the next line still runs.
  ["ls # note\ngh pr list", "gh"],
  ["echo a#b; git push", "git push"],
  // Nested and wrapped substitutions and shells.
  ["bash -lc 'git push'", "git push"],
  ["echo $(echo $(git push))", "git push"],
];

const ALLOWED = [
  "git status",
  "git diff --stat",
  "git add -A",
  "git log --oneline",
  "uv run --no-sync pytest -q",
  "ls -la",
  'echo "git push"',
  "grep -rn gh src",
  "grep -rn https:// src",
  "cat ghost.txt",
  "ls > out.txt",
  "uv run pytest 2>&1",
  "ls # note; gh later",
  "ls # $(git push)",
  "echo '$(git push)'",
  "git stash push",
  "git log --grep push",
  "git",
  "git -C ../repo",
  "command -v gh",
  "command -V curl",
  "command -v curl",
  // AC2 (docs/specs/pi-command-filter.md): quoted text and option forms are not commands.
  "git -C ../repo status",
  "echo 'gh pr list' | wc -l",
  "bash -c 'git status'",
  "",
];

for (const [command, rule] of BLOCKED) {
  test(`blocks ${JSON.stringify(command)} as '${rule}'`, () => {
    const reason = blockedReason(command);
    assert.ok(
      reason?.startsWith(`Blocked by the factory command filter: '${rule}' is not allowed. `),
      `unexpected reason: ${reason}`,
    );
    assert.ok(reason.endsWith(` ${REASON_SUFFIX}`), `unexpected reason: ${reason}`);
  });
}

for (const command of ALLOWED) {
  test(`allows ${JSON.stringify(command)}`, () => {
    assert.equal(blockedReason(command), null);
  });
}

const EXACT_REASONS = [
  [
    "git push",
    "Blocked by the factory command filter: 'git push' is not allowed. " +
      "The factory commits and pushes your changes; leave them in the worktree. " +
      "Do not retry or rephrase this command.",
  ],
  [
    "git commit -m x",
    "Blocked by the factory command filter: 'git commit' is not allowed. " +
      "The factory commits and pushes your changes; leave them in the worktree. " +
      "Do not retry or rephrase this command.",
  ],
  [
    "gh pr list",
    "Blocked by the factory command filter: 'gh' is not allowed. " +
      "The factory handles GitHub. " +
      "Do not retry or rephrase this command.",
  ],
  [
    "curl https://example.com",
    "Blocked by the factory command filter: 'curl' is not allowed. " +
      "Network fetches are not allowed; work with files in the worktree. " +
      "Do not retry or rephrase this command.",
  ],
  [
    "wget example.com",
    "Blocked by the factory command filter: 'wget' is not allowed. " +
      "Network fetches are not allowed; work with files in the worktree. " +
      "Do not retry or rephrase this command.",
  ],
];

for (const [command, reason] of EXACT_REASONS) {
  test(`reason for ${JSON.stringify(command)} names what to do instead`, () => {
    assert.equal(blockedReason(command), reason);
  });
}

const MISSING_COMMAND_REASON =
  "Blocked by the factory command filter: the bash call has no readable command string.";

function registeredHandler() {
  const calls = [];
  filter({ on: (eventName, handler) => calls.push({ eventName, handler }) });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].eventName, "tool_call");
  return calls[0].handler;
}

test("handler blocks a denied bash command with the reason", async () => {
  const result = await registeredHandler()({
    toolName: "bash",
    input: { command: "git push origin HEAD" },
  });
  assert.equal(result.block, true);
  assert.equal(
    result.reason,
    "Blocked by the factory command filter: 'git push' is not allowed. " +
      "The factory commits and pushes your changes; leave them in the worktree. " +
      "Do not retry or rephrase this command.",
  );
});

test("handler lets an allowed bash command through", async () => {
  const result = await registeredHandler()({ toolName: "bash", input: { command: "ls -la" } });
  assert.equal(result, undefined);
});

for (const [label, input] of [
  ["no input", undefined],
  ["no command", {}],
  ["a non-string command", { command: 42 }],
]) {
  test(`handler blocks a bash call with ${label}`, async () => {
    const result = await registeredHandler()({ toolName: "bash", input });
    assert.equal(result.block, true);
    assert.equal(result.reason, MISSING_COMMAND_REASON);
  });
}

test("handler does not filter other tools", async () => {
  const result = await registeredHandler()({
    toolName: "write",
    input: { path: "notes.txt", content: "git push https://x" },
  });
  assert.equal(result, undefined);
});
