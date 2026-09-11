# Writing policy

The factory uses concise controlled English for all text that it authors.

The policy applies to:

- Agent output fields
- Instructions sent to agents
- Generated project issues
- Pull request titles and bodies
- Commit messages

The factory uses selected checks from
[SimpleEnglish](https://github.com/AminBlg/SimpleEnglish) v2.0.2. The included
source is pinned to revision
`61ee200efbd423050aab982eed94226229891ae0`.

## Rules

The factory checks these rules:

- Use 20 words or fewer for an instruction sentence.
- Use 25 words or fewer for a descriptive sentence.
- Keep bounded artifact fields within their total word limit.
- Remove filler terms.
- Do not use semicolons.
- Do not use em dashes.
- Do not use Latin abbreviations such as `e.g.` or `i.e.`.

The prompts also tell agents to use active voice, simple tenses and one idea
per sentence.

## Correction

If model prose fails, the controller sends the exact findings to the same
role. The correction is bounded by `factory.retries.same_model_attempts`.

The controller does not rewrite the artifact. This rule protects facts and
safety language.

## Exact text

The policy does not change:

- Code
- Identifiers
- File paths
- Commands
- URLs
- Quoted errors
- Raw command output
- Human-authored source text

Project child work items receive only their task description. The factory does
not copy the full project brief into every child prompt.

## Compliance limit

The checks follow ASD-STE100 Simplified Technical English principles. They do
not implement the full controlled dictionary or validate word meaning.

No tool can guarantee ASD-STE100 compliance. The official standard is
available from [asd-ste100.org](https://www.asd-ste100.org/).
