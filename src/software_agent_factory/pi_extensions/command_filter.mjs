// Factory command filter for the pi IMPLEMENTER.
//
// Blocks the shell commands the Copilot runtime also denies. It has the same strength as
// Copilot's pattern rules: it reads command positions, and it is not a security boundary.
// Plain ESM with no dependencies, so pi loads it without a build step.

const COMMIT_PUSH_INSTEAD =
  "The factory commits and pushes your changes; leave them in the worktree.";
const NETWORK_INSTEAD = "Network fetches are not allowed; work with files in the worktree.";

const RULES = [
  { name: "git commit", program: "git", subcommand: "commit", instead: COMMIT_PUSH_INSTEAD },
  { name: "git push", program: "git", subcommand: "push", instead: COMMIT_PUSH_INSTEAD },
  { name: "gh", program: "gh", instead: "The factory handles GitHub." },
  { name: "curl", program: "curl", instead: NETWORK_INSTEAD },
  { name: "wget", program: "wget", instead: NETWORK_INSTEAD },
];

const REASON_PREFIX = "Blocked by the factory command filter:";
const REASON_SUFFIX = "Do not retry or rephrase this command.";
const MISSING_COMMAND_REASON = `${REASON_PREFIX} the bash call has no readable command string.`;

// --- Tokenizer -------------------------------------------------------------------------
// Turns a shell string into argv lists, one per simple command. Quotes are removed, and
// command substitutions become commands of their own. Redirects and # comments are dropped.
// Heredoc bodies are checked as commands (accepted limit; use the write tool). It is not a
// full shell parser.

const SEPARATORS = new Set([";", "&", "|", "\n", "(", ")"]);
const REDIRECT_OPERATOR = /^[<>]{1,3}[&|]?/; // <, >, >>, <<, <<<, >&, >|, <&
const FILE_DESCRIPTOR = /^(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})$/; // 2 or {fd}

function closingParen(text, start) {
  let depth = 1;
  for (let i = start; i < text.length; i++) {
    if (text[i] === "(") depth++;
    if (text[i] === ")" && --depth === 0) return i;
  }
  return text.length;
}

export function parseCommands(text) {
  const commands = [];
  let words = [];
  let word = null; // null means no word is in progress; "" is an empty quoted word
  let quote = null;
  let dropNextWord = false; // the word after a redirect operator is a file, not an argument

  const endWord = () => {
    if (word !== null) {
      if (dropNextWord) dropNextWord = false;
      else words.push(word);
    }
    word = null;
  };
  const endSegment = () => {
    endWord();
    dropNextWord = false;
    if (words.length > 0) commands.push(words);
    words = [];
  };
  const substitute = (body) => {
    commands.push(...parseCommands(body));
    word ??= "";
  };

  // The else-if order matters: quote states are tested before the unquoted-only branches.
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote === "'") {
      if (ch === "'") quote = null;
      else word += ch;
    } else if (ch === "\\" && text[i + 1] === "\n") {
      i++; // a backslash-newline joins the lines
    } else if (ch === "\\" && i + 1 < text.length) {
      word = (word ?? "") + text[++i];
    } else if (ch === "$" && text[i + 1] === "(") {
      const closingParenIndex = closingParen(text, i + 2);
      substitute(text.slice(i + 2, closingParenIndex));
      i = closingParenIndex;
    } else if (ch === "`") {
      const closingBacktickIndex =
        text.indexOf("`", i + 1) === -1 ? text.length : text.indexOf("`", i + 1);
      substitute(text.slice(i + 1, closingBacktickIndex));
      i = closingBacktickIndex;
    } else if (ch === '"') {
      quote = quote === '"' ? null : '"';
      word ??= "";
    } else if (quote === '"') {
      word += ch;
    } else if (ch === "'") {
      quote = "'";
      word ??= "";
    } else if (ch === "#" && word === null) {
      while (i + 1 < text.length && text[i + 1] !== "\n") i++; // a comment runs to the newline
    } else if (ch === "<" || ch === ">") {
      // The 2 of 2>&1 and the {fd} of {fd}>out belong to the redirect, not the command.
      if (word !== null && FILE_DESCRIPTOR.test(word)) word = null;
      else endWord();
      i += text.slice(i).match(REDIRECT_OPERATOR)[0].length - 1;
      dropNextWord = true;
    } else if (ch === " " || ch === "\t") {
      endWord();
    } else if (SEPARATORS.has(ch)) {
      endSegment();
    } else {
      word = (word ?? "") + ch;
    }
  }
  endSegment();
  return commands;
}

// --- Command normalisation ------------------------------------------------------------
// Reduces an argv list to the command that really runs: drops VAR=value assignments and
// env/command prefixes, and opens sh -c / bash -c bodies.

const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
// env options that take a value (GNU and BSD). In a short bundle such as -iu, the first
// value letter takes the rest of the word, or the next word when nothing is attached.
const ENV_SHORT_WITH_VALUE = new Set(["u", "C", "S", "P"]);
const ENV_LONG_WITH_VALUE = new Set(["--unset", "--chdir", "--split-string"]);
const COMMAND_LOOKUP_FLAG = /^-[A-Za-z]*[vV][A-Za-z]*$/; // command -v and -V only look a name up
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh"]);
const SHELL_COMMAND_FLAG = /^-[A-Za-z]*c[A-Za-z]*$/;

const baseName = (path) => path.split("/").pop();

function splitOptions(args) {
  let i = 0;
  while (i < args.length && args[i].startsWith("-")) i++;
  return { options: args.slice(0, i), rest: args.slice(i) };
}

// Returns what env runs. A -S / --split-string value is a command line: its words go
// back through env option parsing, as GNU env does, and then run before the rest.
function envCommand(args) {
  const splitString = (value, restIndex) => [
    "env",
    ...(parseCommands(value)[0] ?? []),
    ...args.slice(restIndex),
  ];
  let i = 0;
  while (i < args.length && args[i].startsWith("-") && args[i] !== "-") {
    const arg = args[i];
    if (arg === "--") return args.slice(i + 1);
    if (arg.startsWith("--")) {
      const equals = arg.indexOf("=");
      const name = equals === -1 ? arg : arg.slice(0, equals);
      const attached = equals !== -1;
      if (name === "--split-string") {
        return attached
          ? splitString(arg.slice(equals + 1), i + 1)
          : splitString(args[i + 1] ?? "", i + 2);
      }
      i += ENV_LONG_WITH_VALUE.has(name) && !attached ? 2 : 1;
      continue;
    }
    let consumed = 1;
    for (let j = 1; j < arg.length; j++) {
      if (!ENV_SHORT_WITH_VALUE.has(arg[j])) continue;
      const attachedValue = arg.slice(j + 1);
      if (attachedValue === "") consumed = 2;
      if (arg[j] === "S") {
        const value = attachedValue === "" ? (args[i + 1] ?? "") : attachedValue;
        return splitString(value, i + consumed);
      }
      break;
    }
    i += consumed;
  }
  return args[i] === "-" ? args.slice(i + 1) : args.slice(i);
}

function stripPrefixes(argv) {
  let args = argv;
  for (;;) {
    const firstNonAssignmentIndex = args.findIndex((arg) => !ASSIGNMENT.test(arg));
    args = firstNonAssignmentIndex === -1 ? [] : args.slice(firstNonAssignmentIndex);
    if (args.length === 0) return args;
    const program = baseName(args[0]);
    if (program === "env") {
      args = envCommand(args.slice(1));
    } else if (program === "command") {
      const { options, rest } = splitOptions(args.slice(1));
      if (options.some((option) => COMMAND_LOOKUP_FLAG.test(option))) return [];
      args = rest;
    } else {
      return args;
    }
  }
}

function realCommands(argv) {
  const args = stripPrefixes(argv);
  if (args.length === 0) return [];
  if (SHELLS.has(baseName(args[0]))) {
    const commandFlagIndex = args.findIndex((arg) => SHELL_COMMAND_FLAG.test(arg));
    if (commandFlagIndex !== -1 && commandFlagIndex + 1 < args.length) {
      return parseCommands(args[commandFlagIndex + 1]).flatMap(realCommands);
    }
  }
  return [args];
}

// --- Rules ----------------------------------------------------------------------------

// The --name=value forms are one token, so only the separate-value forms are listed.
const GIT_OPTIONS_WITH_VALUE = new Set([
  "-C",
  "-c",
  "--git-dir",
  "--work-tree",
  "--namespace",
  "--attr-source",
  "--exec-path",
  "--super-prefix",
  "--config-env",
]);

function gitSubcommand(args) {
  let i = 1;
  while (i < args.length && args[i].startsWith("-")) {
    i += GIT_OPTIONS_WITH_VALUE.has(args[i]) ? 2 : 1;
  }
  return args[i];
}

function matchingRule(args) {
  const program = baseName(args[0]);
  return RULES.find(
    (rule) =>
      rule.program === program &&
      (rule.subcommand === undefined || gitSubcommand(args) === rule.subcommand),
  );
}

export function blockedReason(command) {
  for (const argv of parseCommands(command).flatMap(realCommands)) {
    const rule = matchingRule(argv);
    if (rule) {
      return `${REASON_PREFIX} '${rule.name}' is not allowed. ${rule.instead} ${REASON_SUFFIX}`;
    }
  }
  return null;
}

// --- pi extension ---------------------------------------------------------------------

export default (pi) =>
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return undefined;
    const command = event.input?.command;
    const reason = typeof command === "string" ? blockedReason(command) : MISSING_COMMAND_REASON;
    return reason === null ? undefined : { block: true, reason };
  });
