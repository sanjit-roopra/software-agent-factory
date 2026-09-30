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
const FILE_DESCRIPTOR = /^(?:\d+|\{[A-Za-z_]\w*\})$/; // 2 or {fd}

function closingParen(text, start) {
  let depth = 1;
  for (let i = start; i < text.length; i++) {
    if (text[i] === "(") depth++;
    if (text[i] === ")" && --depth === 0) return i;
  }
  return text.length;
}

// One pass over a shell string. Each step consumes at least one character.
class Tokenizer {
  constructor(text) {
    this.text = text;
    this.pos = 0;
    this.commands = [];
    this.words = [];
    this.word = null; // null means no word is in progress; "" is an empty quoted word
    this.quote = null;
    this.dropNextWord = false; // the word after a redirect operator is a file, not an argument
  }

  run() {
    while (this.pos < this.text.length) this.step(this.text[this.pos]);
    this.endSegment();
    return this.commands;
  }

  // The order matters: quote states are tested before the unquoted-only cases.
  step(ch) {
    if (this.quote === "'") return this.singleQuoted(ch);
    if (ch === "\\") return this.backslash();
    if (ch === "$" && this.text[this.pos + 1] === "(") return this.dollarParen();
    if (ch === "`") return this.backtick();
    if (ch === '"') return this.doubleQuote();
    if (this.quote === '"') return this.append(ch);
    return this.unquoted(ch);
  }

  unquoted(ch) {
    if (ch === "'") {
      this.quote = "'";
      this.word ??= "";
      this.pos++;
    } else if (ch === "#" && this.word === null) {
      this.comment();
    } else if (ch === "<" || ch === ">") {
      this.redirect();
    } else if (ch === " " || ch === "\t") {
      this.endWord();
      this.pos++;
    } else if (SEPARATORS.has(ch)) {
      this.endSegment();
      this.pos++;
    } else {
      this.append(ch);
    }
  }

  append(ch) {
    this.word = (this.word ?? "") + ch;
    this.pos++;
  }

  singleQuoted(ch) {
    if (ch === "'") this.quote = null;
    else this.word += ch;
    this.pos++;
  }

  doubleQuote() {
    this.quote = this.quote === '"' ? null : '"';
    this.word ??= "";
    this.pos++;
  }

  backslash() {
    const next = this.text[this.pos + 1];
    if (next === "\n") {
      this.pos += 2; // a backslash-newline joins the lines
    } else if (next === undefined) {
      this.append("\\");
    } else {
      this.word = (this.word ?? "") + next;
      this.pos += 2;
    }
  }

  dollarParen() {
    const close = closingParen(this.text, this.pos + 2);
    this.substitute(this.text.slice(this.pos + 2, close));
    this.pos = close + 1;
  }

  backtick() {
    const found = this.text.indexOf("`", this.pos + 1);
    const close = found === -1 ? this.text.length : found;
    this.substitute(this.text.slice(this.pos + 1, close));
    this.pos = close + 1;
  }

  comment() {
    const newline = this.text.indexOf("\n", this.pos); // a comment runs to the newline
    this.pos = newline === -1 ? this.text.length : newline;
  }

  redirect() {
    // The 2 of 2>&1 and the {fd} of {fd}>out belong to the redirect, not the command.
    if (this.word !== null && FILE_DESCRIPTOR.test(this.word)) this.word = null;
    else this.endWord();
    this.pos += this.text.slice(this.pos).match(REDIRECT_OPERATOR)[0].length;
    this.dropNextWord = true;
  }

  substitute(body) {
    this.commands.push(...parseCommands(body));
    this.word ??= "";
  }

  endWord() {
    if (this.word !== null) {
      if (this.dropNextWord) this.dropNextWord = false;
      else this.words.push(this.word);
    }
    this.word = null;
  }

  endSegment() {
    this.endWord();
    this.dropNextWord = false;
    if (this.words.length > 0) this.commands.push(this.words);
    this.words = [];
  }
}

export function parseCommands(text) {
  return new Tokenizer(text).run();
}

// --- Command normalisation ------------------------------------------------------------
// Reduces an argv list to the command that really runs: drops VAR=value assignments and
// env/command prefixes, and opens sh -c / bash -c bodies.

const ASSIGNMENT = /^[A-Za-z_]\w*=/;
// env options that take a value (GNU and BSD). In a short bundle such as -iu, the first
// value letter takes the rest of the word, or the next word when nothing is attached.
const ENV_SHORT_WITH_VALUE = new Set(["u", "C", "S", "P"]);
const ENV_LONG_WITH_VALUE = new Set(["--unset", "--chdir", "--split-string"]);
const SHORT_OPTION_BUNDLE = /^-[A-Za-z]+$/;
// command -v and -V only look a name up.
const isCommandLookupFlag = (arg) => SHORT_OPTION_BUNDLE.test(arg) && /[vV]/.test(arg);
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh"]);
const isShellCommandFlag = (arg) => SHORT_OPTION_BUNDLE.test(arg) && arg.includes("c");

const baseName = (path) => path.split("/").pop();

function splitOptions(args) {
  let i = 0;
  while (i < args.length && args[i].startsWith("-")) i++;
  return { options: args.slice(0, i), rest: args.slice(i) };
}

function longOption(arg) {
  const equals = arg.indexOf("=");
  return equals === -1
    ? { name: arg, value: null }
    : { name: arg.slice(0, equals), value: arg.slice(equals + 1) };
}

// Reads the env option at args[i]. Returns how many words it uses, and its value when
// it is -S / --split-string, whose value is a command line.
function envOption(args, i) {
  const arg = args[i];
  if (arg.startsWith("--")) {
    const { name, value } = longOption(arg);
    const takesNext = ENV_LONG_WITH_VALUE.has(name) && value === null;
    const optionValue = takesNext ? (args[i + 1] ?? "") : value;
    return {
      consumed: takesNext ? 2 : 1,
      splitValue: name === "--split-string" ? optionValue : null,
    };
  }
  const letterIndex = [...arg].findIndex((letter, j) => j > 0 && ENV_SHORT_WITH_VALUE.has(letter));
  if (letterIndex === -1) return { consumed: 1, splitValue: null };
  const attached = arg.slice(letterIndex + 1);
  const value = attached === "" ? (args[i + 1] ?? "") : attached;
  return {
    consumed: attached === "" ? 2 : 1,
    splitValue: arg[letterIndex] === "S" ? value : null,
  };
}

const endsEnvOptions = (arg) => arg === "-" || arg === "--";

// Returns what env runs. A -S / --split-string value is a command line: its words go
// back through env option parsing, as GNU env does, and then run before the rest.
function envCommand(args) {
  let i = 0;
  while (i < args.length && args[i].startsWith("-") && !endsEnvOptions(args[i])) {
    const { consumed, splitValue } = envOption(args, i);
    if (splitValue !== null) {
      return ["env", ...(parseCommands(splitValue)[0] ?? []), ...args.slice(i + consumed)];
    }
    i += consumed;
  }
  return i < args.length && endsEnvOptions(args[i]) ? args.slice(i + 1) : args.slice(i);
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
      if (options.some(isCommandLookupFlag)) return [];
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
    const commandFlagIndex = args.findIndex(isShellCommandFlag);
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

export default function registerCommandFilter(pi) {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return undefined;
    const command = event.input?.command;
    const reason = typeof command === "string" ? blockedReason(command) : MISSING_COMMAND_REASON;
    return reason === null ? undefined : { block: true, reason };
  });
}
