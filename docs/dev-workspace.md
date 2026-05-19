# Dev Workspace — read-only local Git context (Phase 1)

> **Status: shipped (Phase 1), opt-in, strictly read-only.** A Nova
> Project can optionally link to a local Git checkout so Nova
> understands the repository's state (branch, clean/dirty, recent
> commits, changed files) when helping you code. Phase 1 **observes
> only** — it can never commit, push, branch, fetch, write files, or
> run an arbitrary command. The feature is off until an operator
> configures an allowed workspace root.

This document describes the safety boundaries the feature commits to
and the setup required before anything happens. It sits inside the
limits set by
[`nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md)
and alongside [`projects.md`](projects.md) and
[`maintenance-center.md`](maintenance-center.md); nothing here weakens
those documents or grants Nova new powers.

## Why this exists

To evolve Nova into a local-first development copilot it needs to
*understand* the repository you are working in — what branch you are
on, whether the tree is dirty, what changed, the recent history — so
its help is grounded in your actual code. Phase 1 delivers exactly
that understanding and **nothing else**: it is the read-only
foundation the later phases build on.

## What Phase 1 does

- Lets a project optionally store one **linked local repository path**.
- Validates that path hard (see below) before it is ever stored.
- Exposes a small set of **read-only** Git facts for a linked repo:
  - `git status --short`
  - `git branch --show-current`
  - `git log --oneline -n 20`
  - `git diff --stat`
  - the list of changed files (parsed from `git status --porcelain`)
- Surfaces them in the project's **Dev Workspace** panel (the `⎇`
  button next to the project selector): linked path, current branch,
  clean/dirty badge, latest commits, changed files, diff summary.

## What Phase 1 deliberately does NOT do

- No commit, push, branch creation, merge, rebase, reset, or stash.
- No file writes anywhere in the repository.
- No `git fetch` / `pull` / `clone` / `remote` — **no network calls**
  and no background scans of the filesystem.
- No arbitrary command execution and no shell interpretation.
- No `sudo` / `pkexec` / `doas` / `su` / `runuser`.
- No GitHub / Codeberg API calls.
- No secrets read, stored, or surfaced.
- No autonomous actions of any kind — every read is triggered by you
  opening the panel.

## Safety boundaries (enforced in code)

These are enforced in `core/dev_workspace.py` and covered by
`tests/test_dev_workspace.py`:

1. **Opt-in by the operator.** A repo can only be linked when it
   resolves *inside* one of the absolute directories listed in
   `NOVA_DEV_WORKSPACE_ROOTS`. Unset / empty → the feature is off and
   no path validates. Nova never invents an allowed root.
2. **Hard path validation.** The candidate path must:
   - be a non-empty, absolute string (no `~`, no `..`, no NUL /
     newline, length-capped),
   - resolve (symlinks included) to an existing **directory**,
   - contain a `.git` entry,
   - **not** be `/`, a top-level directory (e.g. `/home`, `/mnt`,
     `/srv`), or any broad system path (`/etc`, `/usr`, `/var`,
     `/root`, …) — even if an operator points a root at one,
   - resolve **inside** a configured allowed root.

   A symlink that lives in an allowed root but points outside it is
   refused, because containment is checked on the *resolved* path.
3. **Allowlisted git only.** Every subprocess is built from a
   hard-coded argv tuple drawn from a frozen allowlist of the
   read-only subcommands above. No user / model / chat input is ever
   concatenated into a command; the validated repo path is used solely
   as the process working directory, never inside argv.
4. **`shell=False` everywhere**, stdin closed, per-call timeouts, and
   `GIT_TERMINAL_PROMPT=0` / `GIT_OPTIONAL_LOCKS=0` so a read can
   never prompt, hang, or take a repository lock.
5. **Calm, capped, sanitised output.** Every list field is line- and
   length-capped. Snapshots never raise and never embed secrets,
   environment variables, raw stderr, or stack traces — only short,
   fixed, frontend-safe summaries (`state` is one of `ready`,
   `disabled`, `invalid_path`, `git_unavailable`, `error`).
6. **User-scoped.** The link and its status are per-project and
   per-user exactly like the rest of the project surface; a foreign /
   unknown project id returns `404` (never `403`) so existence is not
   leaked across accounts.

The linked path is **contextual data only** — storing it confers no
write power. It is surfaced to the model the same way other project
context is: strictly *below* the identity / safety contract in the
system prompt, where it cannot override safety, identity, auth, or
admin rules.

## Setup

The feature is off until you list at least one allowed root. Set, in
your environment / `.env`:

```sh
# OS-path-separator- or comma-separated absolute directories that may
# contain linkable repositories. Pick the parent folder you keep
# checkouts in — never "/", "/home", or a broad system path.
NOVA_DEV_WORKSPACE_ROOTS=/home/me/code:/srv/projects
```

Then, in a project, click the `⎇` button next to the project
selector, paste the **absolute path** of a checkout that lives under
one of those roots, and click **Link**. The panel then shows that
repo's read-only Git state. **Unlink** clears the stored path and
never touches the repository.

## API surface (Phase 1)

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `PUT` | `/projects/{id}/repo` | Body `{ "path": "<abs path>" \| null }`. A non-empty path is validated then stored (resolved); `null` / empty unlinks. Invalid path → `400` with a short reason; foreign project → `404`. |
| `GET` | `/projects/{id}/repo/status` | `{ "linked": false }` when no repo; otherwise a calm read-only snapshot (`state`, `branch`, `clean`, `status_short`, `recent_commits`, `changed_files`, `diff_stat`, `detail`). Foreign project → `404`. |

`GET /projects` and the other project endpoints additionally report
`local_repo_path` and `has_local_repo` so the UI can show which
projects are linked.

## Roadmap (later phases — not in Phase 1)

Each phase is additive and stays behind explicit user confirmation;
nothing below is enabled by Phase 1.

- **Phase 2 — patch proposal mode.** Nova may *propose* a unified diff;
  it is shown for review and never applied automatically.
- **Phase 3 — apply patch with approval.** Apply a reviewed patch to
  the working tree only after explicit per-patch approval.
- **Phase 4 — branch + commit locally.** Create a local feature branch
  and commit, never on `main`, never pushed.
- **Phase 5 — PR draft assistant.** Help draft a PR description from
  the local diff — still no network writes.
- **Phase 6 — optional push after explicit confirmation.** A push,
  only after an unmistakable, per-action confirmation; never to
  `main`, never autonomous.

These are intentionally out of scope for Phase 1 to keep it small,
auditable, and safe.
