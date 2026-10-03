---
name: pr-gate
description: Run the repository's checks before opening a pull request. Use when the user says "open a PR", "ready for review" or "I'm done with this change".
---

# Pull request gate

Run each check from the repository root, in order. Stop at the first failure, fix it, and run all checks again.

```bash
{{verify_commands}}
```

- Do not skip, disable or weaken a check to make it pass.
- Do not change a check's configuration unless the user asks.
- When every check passes, commit, push and open the pull request.
- Put the checks you ran and their results in the pull request description.
