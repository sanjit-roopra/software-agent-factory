import assert from "node:assert/strict";
import { test } from "node:test";

import filter, {
  blockedReason,
} from "../../src/software_agent_factory/pi_extensions/command_filter.mjs";

const SUFFIX = "Do not retry or rephrase this command.";

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
  // Beyond the Gherkin table: other separators and option forms.
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
  // Beyond the Gherkin table.
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
    assert.ok(reason.endsWith(` ${SUFFIX}`), `unexpected reason: ${reason}`);
    assert.ok(!reason.includes(".."), `double period in: ${reason}`);
  });
}

for (const command of ALLOWED) {
  test(`allows ${JSON.stringify(command)}`, () => {
    assert.equal(blockedReason(command), null);
  });
}

test("reason names what to do instead", () => {
  assert.equal(
    blockedReason("git push"),
    "Blocked by the factory command filter: 'git push' is not allowed. " +
      "The factory commits and pushes your changes; leave them in the worktree. " +
      SUFFIX,
  );
});

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
  assert.equal(result.reason, blockedReason("git push origin HEAD"));
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
    assert.equal(typeof result.reason, "string");
  });
}

test("handler does not filter other tools", async () => {
  const result = await registeredHandler()({
    toolName: "write",
    input: { path: "notes.txt", content: "git push https://x" },
  });
  assert.equal(result, undefined);
});
