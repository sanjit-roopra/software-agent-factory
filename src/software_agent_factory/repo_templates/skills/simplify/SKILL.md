---
name: simplify
description: Make a finished, passing change smaller and clearer without changing its behavior. Use after the tests pass and before review.
---

# Simplify

Work only on the files the change touched. Keep behavior the same.

1. Remove code the change no longer needs: dead branches, unused parameters, unused imports.
2. Reuse an existing helper instead of a new copy of the same logic.
3. Flatten deep nesting with early returns.
4. Split a function that does more than one job.
5. Replace a clever expression with a plain one when the plain one is as short.

After each step, run the `pr-gate` checks. Undo a step that makes a check fail.
Do not add features, abstractions or configuration.
