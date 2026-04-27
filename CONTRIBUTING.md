# Contributing

Thanks for your interest in contributing.

## Branch rules

- Do NOT push directly to `main`
- Do NOT use or push to `dev/thezupzup-private` (this branch is reserved for the maintainer)

Instead, always create your own branch from `main`:

```bash
git checkout main
git pull origin main
git checkout -b feature/your-change-name
```

All changes must go through a Pull Request to main.

Before opening a PR:

git fetch origin
git rebase origin/main
Important rules
One change per PR (keep it small)
Do not modify unrelated files
Do not change LICENSE or packaging unless required
Keep the code simple and readable
Maintainer branch

The branch dev/thezupzup-private is used only by the maintainer for fast development.

Please do not use it.

Summary
Work on your own branch
Open a PR to main
Keep changes small and clean
