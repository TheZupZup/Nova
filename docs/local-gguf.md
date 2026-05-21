# Running Nova with a local GGUF model (llama.cpp)

> **Status: shipped (Phase 1 + Phase 2 + Phase 3, optional, local-first).**
> This document describes the optional `llamacpp` model provider, which
> lets Nova generate text from a local `.gguf` model **without Ollama**.
> Phase 1 added the provider itself; **Phase 2 adds a small admin-only UI
> for setting and validating the model path** (Settings → Models → "Local
> GGUF model") so you no longer have to edit `.env` by hand; **Phase 3
> adds a read-only model library** that discovers the `.gguf` files
> already inside the model directory so an admin can pick one instead of
> typing its full path. It lives inside the boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md)
> and complements [`docs/model-providers.md`](model-providers.md), which
> explains the provider seam itself. **Ollama remains Nova's default and
> is fully supported** — this provider is opt-in and changes nothing
> until you select it.

## Why this exists

Until now Nova needed [Ollama](https://ollama.com) to turn a prompt into
tokens. The model backend is a [replaceable provider](model-providers.md),
so Nova can instead load a quantised `.gguf` model directly through
[`llama-cpp-python`](https://github.com/abetlen/llama-cpp-python) (Python
bindings for [llama.cpp](https://github.com/ggml-org/llama.cpp)). This
makes Ollama **no longer architecturally required**: you can run Nova on
a host that has only a model file and the `llama-cpp-python` wheel.

What this provider deliberately does **not** do:

- it does **not** remove or replace Ollama (still the default),
- it does **not** download, fetch, or auto-convert any model,
- it does **not** add a cloud provider, an API key, or a model-download manager,
- it does **not** add a general filesystem browser — the Phase-3 model
  library is a **read-only**, **bounded** listing confined to the one
  configured model directory (`NOVA_MODEL_DIR`); it lists only `.gguf`
  files, never the wider filesystem, never follows symlinks out of the
  directory, and never browses an arbitrary path,
- it does **not** delete or overwrite any model file,
- it does **not** change memory, projects, storage, export/restore, or
  the Dev Workspace.

## Ollama is still supported

Nothing about the default deployment changes. Leave `NOVA_MODEL_PROVIDER`
unset (or `=ollama`) and Nova behaves exactly as before — same Ollama
client, same models, same streaming. The GGUF provider is only used when
you explicitly set `NOVA_MODEL_PROVIDER=llamacpp`. You can switch back at
any time by changing the one environment variable and restarting Nova; no
data is migrated and nothing is deleted.

## Prerequisites

1. **The `llama-cpp-python` wheel.** It is intentionally *not* in Nova's
   `requirements.txt` — Nova must install and import cleanly on hosts
   that will only ever use Ollama. Install it yourself:

   ```bash
   pip install llama-cpp-python
   ```

   Prebuilt CPU wheels exist for common platforms. For GPU offload
   (CUDA, Metal, ROCm, Vulkan) follow the project's build instructions —
   the wheel must be compiled with the matching backend before
   `NOVA_GGUF_GPU_LAYERS` has any effect. If the wheel is missing, Nova
   does not crash: the provider reports a clean "not installed" health
   failure and chat degrades to the usual "backend unreachable" reply.

2. **A local `.gguf` model file that you already have.** Nova never
   downloads one. Obtain a quantised model (for example from a model hub
   you trust) out of band and place it on disk yourself. A 7–8B model
   quantised to `Q4_K_M` is a sensible starting point for CPU hosts.

### Recommended model directory

Keep models out of the Nova checkout and the data directory, on storage
with room to spare. Create the directory and drop your `.gguf` file in:

```bash
# Create the recommended directory (or set NOVA_MODEL_DIR to your own).
sudo mkdir -p /mnt/archive/nova-models
# Make it readable by the user Nova runs as (adjust the user/group).
sudo chown "$USER" /mnt/archive/nova-models

# Place a .gguf model you already have inside it (Nova never downloads one).
cp ~/Downloads/mistral-7b-instruct-v0.2.Q4_K_M.gguf /mnt/archive/nova-models/
```

```
/mnt/archive/nova-models/
└── mistral-7b-instruct-v0.2.Q4_K_M.gguf
```

Use a stable mount path defined in `/etc/fstab` (not a transient
`/run/media/...` desktop mount) so a long-running service can always
find the file. Override the location with `NOVA_MODEL_DIR` if
`/mnt/archive/nova-models` does not suit your host.

This directory is the **allowed model directory**: the admin UI (and the
configured path) only accept a model file that resolves *inside* it. Nova
never lists, scans, browses, or downloads into it, and never exposes its
contents through the UI — it validates exactly the one path you point it
at. Putting your model here is what makes the Phase-2 "set the path from
Settings" flow work.

## Configuration

All knobs are environment variables (read at startup, like the rest of
Nova's config). Only `NOVA_GGUF_MODEL_PATH` is required.

| Variable | Default | Description |
|---|---|---|
| `NOVA_MODEL_PROVIDER` | `ollama` | Set to `llamacpp` to use this provider. Any other value (or unset) keeps Ollama. |
| `NOVA_MODEL_DIR` | `/mnt/archive/nova-models` | Directory local `.gguf` files must live inside. The admin UI only accepts a model path that resolves inside this directory (no arbitrary files, no path traversal). |
| `NOVA_GGUF_MODEL_PATH` | — | Absolute path to a local `.gguf` model file. Empty leaves the provider unconfigured (a clean health failure, never a crash). An admin-set path (below) takes precedence over this. |
| `NOVA_GGUF_CONTEXT_SIZE` | `4096` | Context window (`n_ctx`). A non-positive or unparseable value falls back to `4096`. |
| `NOVA_GGUF_THREADS` | `0` | CPU threads (`n_threads`). `0` lets llama.cpp choose. |
| `NOVA_GGUF_GPU_LAYERS` | `0` | Layers to offload to GPU (`n_gpu_layers`). `0` keeps inference on CPU. Requires a GPU-enabled `llama-cpp-python` build. |

Example `.env`:

```bash
NOVA_MODEL_PROVIDER=llamacpp
NOVA_MODEL_DIR=/mnt/archive/nova-models
NOVA_GGUF_MODEL_PATH=/mnt/archive/nova-models/mistral-7b-instruct-v0.2.Q4_K_M.gguf
NOVA_GGUF_CONTEXT_SIZE=4096
# NOVA_GGUF_THREADS=8         # optional; 0 = auto
# NOVA_GGUF_GPU_LAYERS=35     # optional; needs a GPU build of llama-cpp-python
```

After editing `.env`, restart Nova. To confirm the backend, open
**Settings → Models** (admin) and click **Test connection** — it runs a
cheap, read-only liveness probe. With the GGUF provider configured the
probe reports the model's filename when the dependency and path are in
place, or a clear reason when either is missing.

## Setting the model path from the UI (no `.env` edit) — Phase 2

You do not have to edit `.env` to point Nova at a model. As an admin,
open **Settings → Models → "Local GGUF model"**. The card shows:

- the **configured provider** (and whether the GGUF provider is active),
- the **model directory** (`NOVA_MODEL_DIR`) and whether it exists,
- the **configured model path**, where it came from (saved here, from the
  environment, or not set), and whether it currently passes validation.

Paste an absolute path to a `.gguf` file **inside the model directory**
and click **Validate & save path**. The server validates the path before
storing it; the saved value is kept in Nova's database and **takes
precedence** over `NOVA_GGUF_MODEL_PATH`, taking effect on the next
message without a restart. Click **Test GGUF provider** to check the
model is valid enough to attempt loading (path is valid *and*
`llama-cpp-python` is installed) — it never loads the multi-GB weights.

A path is accepted only when **all** of these hold; otherwise the save is
refused with a short, sanitised reason and nothing is written:

- it is an **absolute** path containing no `..` and no `~`,
- it ends in **`.gguf`**,
- it resolves (symlinks included) **inside** `NOVA_MODEL_DIR`,
- the file **exists**, is a **regular file** (not a directory), and is
  **readable** by the Nova service user.

The setting is host-wide (an operator decision, like the default model),
admin-only, and never reachable through the per-user settings path. It
never downloads, deletes, or overwrites a model.

## Picking a model from the library (no path typing) — Phase 3

Pasting a full path works, but Phase 3 makes it optional. As an admin,
open **Settings → Models → "Local GGUF model"** and, under **Local model
library**, click **List local models**. Nova lists the `.gguf` files it
finds inside `NOVA_MODEL_DIR`, each with its:

- **file name** and **relative path** (relative to the model directory —
  never an absolute path),
- **size** and **last-modified** time,
- whether it is the **currently selected** model.

Click **Use this model** next to any entry to make it the configured GGUF
model. Selecting is the same validated, persisted action as pasting the
path: the chosen relative path is joined to `NOVA_MODEL_DIR` and
re-validated (it must resolve inside the directory, be a readable regular
`.gguf` file, contain no `..`) before the host-wide setting is written and
the provider is rebuilt on the next message. A model only appears in the
library if it would also pass the paste-a-path validation, so **the listed
set is exactly the selectable set**.

The listing is deliberately conservative — this is *not* a general file
browser:

- it is **read-only**: nothing is created, moved, downloaded, deleted, or
  overwritten, and no shell is ever invoked;
- it is **confined to `NOVA_MODEL_DIR`**: the wider filesystem is never
  scanned, and no caller-supplied path is ever listed;
- the recursion is **bounded**: it descends a limited number of levels,
  visits a capped number of directories, and returns a capped number of
  files (hitting a bound is reported as a `truncated` flag plus a
  warning), so it can never become a filesystem-wide walk;
- it **skips hidden / dot files and directories** and **never follows a
  symlink out of the model directory** (symlinked directories are not
  descended; a symlinked file whose target escapes the directory is
  omitted);
- it lists **only `.gguf` files** and returns **only relative paths +
  safe metadata**, so no unrelated filesystem layout is exposed.

The relevant admin-only endpoints are `GET /admin/provider/gguf/models`
(the listing) and `POST /admin/provider/gguf/select` (pick one by its
relative path). Both require the admin role; non-admins never see the card.

## How model selection interacts

The GGUF provider serves a *single* model — the file at
`NOVA_GGUF_MODEL_PATH`. llama.cpp ignores the per-request model name, so
Nova's routing (`simple` / `normal` / `code` / `advanced`) and the
admin "default model" selection do not change which weights answer; they
all resolve to the one loaded file. The provider reports that file's
basename through the standard health probe, so it appears (and is
selectable) in the default-model surface without any provider-specific
UI code.

## Hardware expectations

llama.cpp runs on CPU by default; a GPU only helps if you installed a
GPU-enabled wheel and set `NOVA_GGUF_GPU_LAYERS`. Rough guidance for
common quantisations:

| Model size | Quant | Approx. RAM (CPU) | Notes |
|---|---|---|---|
| 7–8B | `Q4_K_M` | ~6–8 GB | Comfortable on a 16 GB host; good first choice. |
| 7–8B | `Q5_K_M` / `Q6_K` | ~8–10 GB | Higher quality, more memory. |
| 13–14B | `Q4_K_M` | ~10–12 GB | Wants 16–32 GB; slower on CPU. |
| 30B+ | `Q4_K_M` | 24 GB+ | Practical only with a capable GPU or a large-RAM host. |

These are approximate — actual footprint depends on the model,
quantisation, and `NOVA_GGUF_CONTEXT_SIZE` (a larger context costs more
memory). The model is loaded **lazily on the first message** and then
kept in memory, so the first reply after a restart is slower while
weights load. If a host lacks the memory for the configured model, the
load fails with a sanitised error and chat falls back to the standard
"backend unreachable" reply — Nova stays up.

## Safety notes

- **No downloads.** Nova never fetches a model. You provide the file.
- **Directory-confined paths.** The admin UI (and the validation behind
  it) only accepts a model path that resolves inside `NOVA_MODEL_DIR`,
  defeating path traversal and arbitrary-file exposure. Symlinks are
  resolved before the containment check, so a link that escapes the
  directory is refused.
- **No general file browser.** The Phase-3 model library is a read-only,
  bounded listing confined to `NOVA_MODEL_DIR`: it lists only `.gguf`
  files, never the wider filesystem, skips hidden/system entries, never
  follows a symlink out of the directory, caps its depth / breadth /
  result count, and returns only relative paths and safe metadata. The
  provider never runs a shell command.
- **No deletion, no overwrite.** Configuring a path only records *which*
  file to use; Nova never removes or replaces a model file.
- **Admin-only, host-wide.** The model path is an operator decision (a
  single global setting), gated to admins, and never reachable through
  the per-user settings path.
- **Sanitised errors.** Operator-facing messages name the relevant
  environment variable and the problem; they never echo the absolute
  model path or a raw backend exception. Full detail is in the server
  logs only.
- **Ollama untouched.** Selecting `llamacpp` is a non-destructive runtime
  switch. It never writes settings, never migrates data, and never
  changes Ollama's configuration.
- **Identity still wins.** Like every provider, the GGUF backend only
  turns the messages Nova assembled into text. It cannot reorder or
  override Nova's identity / safety contract — that ordering is owned
  upstream in `core.chat.build_messages`. See
  [`docs/model-providers.md`](model-providers.md).

## Troubleshooting

| Symptom (Test connection / chat) | Likely cause | Fix |
|---|---|---|
| "llama-cpp-python is not installed" | The optional wheel is missing | `pip install llama-cpp-python` in Nova's environment |
| "No GGUF model configured" / "No GGUF model is configured" | No path set (env or UI) | Set `NOVA_GGUF_MODEL_PATH`, or paste a path in Settings → Models |
| "must be a .gguf file" / "must point at a .gguf model file" | Wrong extension | Point at the actual `.gguf` file, not a directory or other format |
| "must be inside the configured model directory" | Path is outside `NOVA_MODEL_DIR` | Move the model into the model directory, or set `NOVA_MODEL_DIR` to where it lives |
| "must not contain '..'" / "must be an absolute path" | Relative or traversal path pasted in the UI | Paste a full absolute path inside the model directory |
| "No file exists at that path" / "GGUF model file not found" | Path typo / file moved / disk not mounted | Check the path; confirm the mount is up |
| "not readable" | Permissions | Make the file readable by the Nova service user |
| "Failed to load the GGUF model" | Corrupt file or not enough memory | Replace the model out of band; try a smaller quant; check free RAM |
