# Dev Workspace — read-only Git context (Phase 1) + patch proposal mode (Phase 2)

> **Status: shipped (Phase 1 + Phase 2), opt-in, never modifies your
> repo.** A Nova Project can optionally link to a local Git checkout so
> Nova understands the repository's state (branch, clean/dirty, recent
> commits, changed files) when helping you code. **Phase 1** observes
> only. **Phase 2** lets Nova *propose* code changes as a reviewable
> patch — an implementation plan, the files it would touch, a
> unified-diff preview, suggested tests, and a risk checklist — that is
> **displayed for review only and never applied**. Neither phase can
> commit, push, branch, fetch, write files, or run an arbitrary
> command. The feature is off until an operator configures an allowed
> workspace root.

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

## What Phase 2 does (patch proposal mode)

Phase 2 lets Nova help you *plan and propose* a code change for a
linked project — but it still **cannot modify your files**. Given a
structured change description from the model, the
`core.dev_workspace.build_patch_proposal` helper returns a calm,
validated, **review-only** object containing:

- an optional short **title** and a one-line **summary**,
- an **implementation plan**,
- the **files likely to change** (repo-relative paths) with a per-file
  added / removed line count,
- a **unified-diff preview** per file plus a combined preview,
- **suggested tests**, and
- a **risk / warning checklist**.

Every built proposal additionally carries a transient `id` (random
UUID) and a UTC `created_at` ISO timestamp so the review UI can pin a
preview to the exact build it is rendering. **No proposal is stored**
in this phase — the values are recomputed on every call, and there is
no read-back endpoint.

The diff is computed locally with Python's `difflib` from the
before/after text the model supplies; Nova does not read your working
tree to build it, so a proposal cannot surface file contents the model
was not already given. **Binary content is refused**: any change whose
`old_content` or `new_content` contains a NUL byte fails validation
with a short reason — there is no readable text diff for a binary
blob, and reviewing binary changes is a deliberately later phase. The
result always carries `review_only: true`, `applied: false`, and a
fixed safety note restating that nothing was written. You review it
and apply it yourself — an explicit, per-patch *apply* step is a
deliberately separate, later phase.

A linked project surfaces a **Patch proposal preview** section inside
the Dev Workspace panel (`⎇`) where you can paste a structured
proposal, validate it through the same backend logic, and review the
resulting title, summary, plan, affected files, diff, tests, and
warnings side by side. Two buttons — **Copy patch** and
**Copy test plan** — copy the combined unified diff and the suggested
tests to your clipboard so you can apply them yourself in your editor
or terminal. There is intentionally no "Apply" button in this phase.

It is reachable per linked project at either
`POST /projects/{id}/repo/patch-proposal` (the original Phase 2 path)
or `POST /projects/{id}/patch-proposals/validate` (the spec-suggested
"validate-only" alias). Both share the same body, scope, and response.

## What Phase 1 and Phase 2 deliberately do NOT do

- No applying a patch, no file writes anywhere in the repository.
- No commit, push, branch creation, merge, rebase, reset, or stash.
- No `git fetch` / `pull` / `clone` / `remote` — **no network calls**
  and no background scans of the filesystem.
- No arbitrary command execution and no shell interpretation. Phase 2
  is a *pure transform* — it spawns no process and touches no git.
- No `sudo` / `pkexec` / `doas` / `su` / `runuser`.
- No GitHub / Codeberg API calls.
- No secrets read, stored, or surfaced; a proposal can never target a
  secret/private file (see the safety boundaries below).
- No autonomous actions of any kind — every read and every proposal is
  triggered by you.

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
6. **User-scoped.** The link, its status, and any patch proposal are
   per-project and per-user exactly like the rest of the project
   surface; a foreign / unknown project id returns `404` (never `403`)
   so existence is not leaked across accounts.
7. **Patch proposals are a pure, review-only transform (Phase 2).**
   `build_patch_proposal` re-validates the linked repo with the same
   hard rules as Phase 1, then for every proposed change:
   - the path must be **repo-relative** — an absolute path, a `~`, a
     `\` separator, a NUL/newline, or any `..` traversal is refused;
   - the path may not target a **secret/private file**: `.env` /
     `*.env` (documented `.env.example`/`.sample`/`.template`/`.dist`
     samples are allowed), `nova.db` / `*.db` / `*.sqlite*`, key
     material (`*.pem`, `*.key`, `*.pfx`, `*.p12`, SSH keys,
     `.ssh` / `.gnupg` / `.aws` / `.kube` / `.docker`), credentials /
     tokens, logs, `*.bak` / `*.backup` / `*.save`, and the private
     Nova runtime dirs (`data`, `backups`, `exports`, `memory-packs`,
     `logs`, `.git`, `__pycache__`, `.venv`, `node_modules`);
   - the path must still resolve **inside** the linked repo (a
     symlinked subdir pointing outside it is refused);
   - **binary content is refused** — any `old_content` /
     `new_content` carrying a NUL byte fails validation, since there
     is no readable text diff for a binary blob;
   - the diff is built locally with `difflib` from the model-supplied
     before/after text — **no git, no subprocess, no file I/O, no
     network**; nothing is applied, staged, committed, pushed, or
     branched, and every field is capped. The result restates
     `review_only: true` / `applied: false`, and includes a transient
     random `id` + UTC `created_at` so the UI can pin a preview to a
     specific build (no proposal is persisted in this phase).
8. **No "Apply" affordance in the UI.** The patch proposal preview
   surface inside the Dev Workspace panel only renders the result and
   offers **Copy patch** / **Copy test plan** clipboard helpers.
   There is no button, endpoint, or code path in this phase that
   could write the proposed diff to disk.

The linked path **and any patch proposal** are **contextual data
only** — they confer no write power. They are surfaced to the model
the same way other project context is: strictly *below* the identity /
safety contract in the system prompt, where they cannot override
safety, identity, auth, or admin rules. A proposal is a suggestion you
review and apply yourself.

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

## API surface (Phase 1 + Phase 2)

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `PUT` | `/projects/{id}/repo` | Body `{ "path": "<abs path>" \| null }`. A non-empty path is validated then stored (resolved); `null` / empty unlinks. Invalid path → `400` with a short reason; foreign project → `404`. |
| `GET` | `/projects/{id}/repo/status` | `{ "linked": false }` when no repo; otherwise a calm read-only snapshot (`state`, `branch`, `clean`, `status_short`, `recent_commits`, `changed_files`, `diff_stat`, `detail`). Foreign project → `404`. |
| `POST` | `/projects/{id}/repo/patch-proposal` | Body is the model's structured change description: `{ "title"?, "summary"?, "plan"?: [str], "changes": [{ "path", "action"?: "modify"\|"add"\|"delete", "old_content"?, "new_content"? }], "tests"?: [str], "risks"?: [str], "warnings"?: [str] }`. Returns a **review-only** `PatchProposal` (`review_only`, `applied:false`, `id`, `created_at`, `title`, `summary`, `plan`, `files[].diff`, `diff_preview`, `suggested_tests`, `risks`, `warnings`, `safety`). Nothing is written. No linked repo → `400`; any invalid path / proposal → `400` with a short reason; foreign project → `404`; an extra body field → `422`. |
| `POST` | `/projects/{id}/patch-proposals/validate` | Spec-suggested alias of the row above: same body, same per-project / per-user scope, same response. Naming the path `.../validate` makes the "we are only validating, nothing is applied" intent explicit at the URL level. |

`GET /projects` and the other project endpoints additionally report
`local_repo_path` and `has_local_repo` so the UI can show which
projects are linked.

## Roadmap

Each phase is additive and stays behind explicit user confirmation;
nothing below is enabled until its phase ships.

- **Phase 1 — read-only Git context.** *Shipped.* Observe a linked
  repo's state; never modify it.
- **Phase 2 — patch proposal mode.** *Shipped.* Nova may *propose* a
  validated unified diff (title, summary, plan, files, tests,
  warnings/risks, transient `id` + `created_at`); it is shown for
  review only — backed by a Dev Workspace preview surface with
  **Copy patch** / **Copy test plan** clipboard helpers, and reachable
  at either `POST /projects/{id}/repo/patch-proposal` or
  `POST /projects/{id}/patch-proposals/validate`. Binary content is
  refused. Patches are **never applied** — no writes, no git, no
  process, no persistence.
- **Phase 3 — apply patch with approval.** *Not yet.* Apply a reviewed
  patch to the working tree only after an explicit, per-patch approval;
  still no commit / push / branch.
- **Phase 4 — branch + commit locally.** *Not yet.* Create a local
  feature branch and commit, never on `main`, never pushed.
- **Phase 5 — PR draft assistant.** *Not yet.* Help draft a PR
  description from the local diff — still no network writes.
- **Phase 6 — optional push after explicit confirmation.** *Not yet.*
  A push, only after an unmistakable, per-action confirmation; never to
  `main`, never autonomous.

The safety model is cumulative: each phase keeps every guarantee of the
phases before it (opt-in operator root, hard path validation, no secret
files, user scoping, capped/sanitised output) and only adds the one
narrowly-scoped capability named in that phase, always behind explicit
user action. Phases 3-6 are intentionally out of scope today to keep
the surface small, auditable, and safe.
