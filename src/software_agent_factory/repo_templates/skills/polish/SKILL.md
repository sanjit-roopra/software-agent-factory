---
name: polish
description: Improve names, comments and idioms of a finished, passing change. Use after simplify and before review.
---

# Polish

Work only on the files the change touched. Keep behavior the same.

- Use names that say what a value is or what a function does. Remove abbreviations that a new reader would not know.
- Keep comments that explain why. Remove comments that repeat the code.
- Follow the idioms the repository already uses, such as its error handling and its test style.
- Add types where the repository uses types.
- Keep public interfaces stable unless the change is about them.

Run the `pr-gate` checks when you finish.
