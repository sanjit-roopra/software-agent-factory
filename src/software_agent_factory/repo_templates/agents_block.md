<!-- factory:begin (managed by software-agent-factory; text inside these markers is replaced) -->
## Development commands

Run these checks before you open a pull request:

```bash
{{verify_commands}}
```

## Skills

Shared skills live in `.agents/skills/`. Claude Code reads them through the links in `.claude/skills/`.

- `pr-gate`: run the checks above and fix every failure before a pull request.
- `simplify`: make a finished change smaller and clearer without changing behavior.
- `polish`: improve names, comments and idioms of a finished change.
<!-- factory:end -->
