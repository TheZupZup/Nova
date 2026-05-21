# Running Nova with a local GGUF model (llama.cpp)

> **Status: shipped (Phase 1, optional, local-first).**
> This document describes the optional `llamacpp` model provider, which
> lets Nova generate text from a local `.gguf` model **without Ollama**.
> It lives inside the boundaries set by
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

What this phase deliberately does **not** do:

- it does **not** remove or replace Ollama (still the default),
- it does **not** download, fetch, or auto-convert any model,
- it does **not** add a cloud provider, an API key, or a model-manager UI,
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
with room to spare:

```
/mnt/archive/nova-models/
└── mistral-7b-instruct-v0.2.Q4_K_M.gguf
```

Use a stable mount path defined in `/etc/fstab` (not a transient
`/run/media/...` desktop mount) so a long-running service can always
find the file. This directory is **only** read by the GGUF provider when
you point `NOVA_GGUF_MODEL_PATH` at a file inside it; Nova never lists,
scans, or browses it, and never exposes its contents through the UI.

## Configuration

All knobs are environment variables (read at startup, like the rest of
Nova's config). Only `NOVA_GGUF_MODEL_PATH` is required.

| Variable | Default | Description |
|---|---|---|
| `NOVA_MODEL_PROVIDER` | `ollama` | Set to `llamacpp` to use this provider. Any other value (or unset) keeps Ollama. |
| `NOVA_GGUF_MODEL_PATH` | — | Absolute path to a local `.gguf` model file. Empty leaves the provider unconfigured (a clean health failure, never a crash). |
| `NOVA_GGUF_CONTEXT_SIZE` | `4096` | Context window (`n_ctx`). A non-positive or unparseable value falls back to `4096`. |
| `NOVA_GGUF_THREADS` | `0` | CPU threads (`n_threads`). `0` lets llama.cpp choose. |
| `NOVA_GGUF_GPU_LAYERS` | `0` | Layers to offload to GPU (`n_gpu_layers`). `0` keeps inference on CPU. Requires a GPU-enabled `llama-cpp-python` build. |

Example `.env`:

```bash
NOVA_MODEL_PROVIDER=llamacpp
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

- **No downloads.** Nova never fetches a model in this phase. You provide
  the file.
- **No shell, no scan.** The provider never runs a shell command and
  never walks the filesystem. It validates exactly the one path you
  configured (readable regular `.gguf` file) and refuses anything else.
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
| "No GGUF model configured" | `NOVA_GGUF_MODEL_PATH` is empty | Set it to your `.gguf` file |
| "must point at a .gguf model file" | Wrong extension | Point at the actual `.gguf` file, not a directory or other format |
| "GGUF model file not found" | Path typo / file moved / disk not mounted | Check the path; confirm the mount is up |
| "model file is not readable" | Permissions | Make the file readable by the Nova service user |
| "Failed to load the GGUF model" | Corrupt file or not enough memory | Re-download the model out of band; try a smaller quant; check free RAM |
