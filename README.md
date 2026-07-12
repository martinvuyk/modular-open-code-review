# Modular Open Code Review

Plug-and-play GitHub Actions for automated PR code review using:

- [Open Code Review](https://github.com/alibaba/open-code-review) (OCR) v1.7.4
- [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) v0.8.1
- [Modular MAX](https://github.com/modular/modular) 26.4.0 with Qwen models (Apache 2.0, no license gate)

Runs on pinned **ubuntu-24.04** with diff-aware model selection and a preflight phase before loading the LLM.

## Quick start

Add two workflow files to your repository. Pin a release tag (e.g. `@v1.0.0`) in production; use `@main` only if you want the latest unreleased changes.

### 1. PR review (required)

Copy [`examples/consumer-workflow.yml`](examples/consumer-workflow.yml) to `.github/workflows/llm-code-review.yml`:

```yaml
name: LLM Code Review
on:
  pull_request_target:
    types: [opened, synchronize, reopened]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    uses: martinvuyk/modular-open-code-review/.github/workflows/review-pr.yml@main
    secrets: inherit
```

### 2. Index warmer (recommended)

Copy [`examples/consumer-index-workflow.yml`](examples/consumer-index-workflow.yml) to `.github/workflows/llm-index.yml`:

```yaml
name: LLM Code Review Index
on:
  push:
    branches: [main]
jobs:
  index:
    uses: martinvuyk/modular-open-code-review/.github/workflows/index-base-branch.yml@main
```

## How it works

```text
PR commit
  → restore CBM index cache (base branch SHA)
  → incremental codebase-memory-mcp index (Phase A, no LLM)
  → ocr review --preview → token estimate → pick model
  → start max serve locally (Phase B)
  → ocr review with MCP + MAX
  → post inline PR comments
```

OCR and codebase-memory-mcp **must run on the same runner** (stdio MCP). MAX runs as a local HTTP server on the same job after preflight completes.

## Workflow inputs

You can leave the defaults or **choose the model** yourself:

| How | What to set |
|-----|-------------|
| Auto (default) | Nothing — local MAX picks the first fitting candidate from [`config/models.cpu.json`](config/models.cpu.json) |
| Force a local Hugging Face model | `model_override: org/model-id` |
| Hosted / external API | `llm_url` + `model_override` (model name the API expects) |
| Change the local candidate list | Edit [`config/models.cpu.json`](config/models.cpu.json), or set `allow_gated_models: true` |

| Input | Default | Description |
|-------|---------|-------------|
| `ocr_version` | `1.7.4` | OCR npm version |
| `cbm_version` | `0.8.1` | codebase-memory-mcp npm version |
| `modular_version` | `26.4.0` | Modular pip package |
| `runner` | `ubuntu-24.04` | Runner label |
| `model_override` | `""` | Hugging Face model ID (local MAX) or API model name (with `llm_url`). Empty = use the CPU fallback chain. |
| `max_estimated_tokens` | `500000` | Skip review when preflight estimate exceeds this |
| `post_comments` | `true` | Post GitHub review comments |
| `llm_url` | `""` | External OpenAI-compatible API (skips local MAX, its caching, and RAM gating). Set `model_override` to name the external model. |
| `llm_extra_body` | `""` | JSON merged into every LLM request. Only for thinking-capable models, e.g. `{"chat_template_kwargs": {"enable_thinking": false}}`. Leave empty for Qwen2.5. |
| `cache_models` | `true` | Cache the MAX venv + weights/compile artifacts (local MAX only; no effect with `llm_url`) |
| `allow_gated_models` | `false` | Include license-gated candidates (e.g. Llama 3.1) in the local fallback chain. Default keeps only ungated Apache-2.0 models. |
| `debug_review` | `false` | Print OCR session trace in the job log and upload `ocr-session` JSONL artifact (`OCR_CONTENT_LOGGING` to stderr). |
| `action_ref` | `main` | Ref of this repo for scripts |

### Secrets

The default ungated Qwen models do **not** need secrets. Add a repository secret when you override to a gated Hub model, raise rate limits, or otherwise need authenticated Hugging Face downloads.

| Secret | Required | Description |
|--------|----------|-------------|
| `HF_TOKEN` | Optional | [Hugging Face access token](https://huggingface.co/settings/tokens). Forwarded to `max serve` via `secrets: inherit` in the consumer workflow. |

Keep `secrets: inherit` on the reusable workflow call (as in the quick-start example) so `HF_TOKEN` reaches the job when present.

### `max_estimated_tokens`

This is a **preflight safety cap**, not an OCR or API billing limit. Before loading the LLM, we estimate prompt size from the git diff plus per-file/bundle overhead (see [`scripts/estimate-review-tokens.sh`](scripts/estimate-review-tokens.sh)). The default matches `max_tokens` in [`config/models.cpu.json`](config/models.cpu.json) and is meant to avoid OOM/timeouts on 16 GB GitHub-hosted runners.

Rough scale (heuristic, not exact):

| PR shape | Approx. estimate |
|----------|------------------|
| ~500 changed lines, few files | ~50k–80k |
| ~5k changed lines, ~20 files | ~150k–250k |
| ~50+ changed files (even modest diffs) | ~400k+ (per-file overhead dominates) |

Example — pin a model and lower the preflight cap:

```yaml
jobs:
  review:
    uses: martinvuyk/modular-open-code-review/.github/workflows/review-pr.yml@main
    with:
      model_override: Qwen/Qwen2.5-1.5B-Instruct
      max_estimated_tokens: '300000'
    secrets: inherit
```

`model_override` bypasses the fallback chain and loads that single model as `float32` safetensors (no GGUF/`--weight-path`), so only use it for models that fit your runner as `float32`, or on a larger runner. For a stronger quantized model, prefer editing the chain in [`config/models.cpu.json`](config/models.cpu.json) or setting `allow_gated_models: true`.

## Default Qwen2.5 CPU models

On GitHub-hosted **CPU** runners with Modular MAX **26.4**, the practical local defaults are small **Qwen2.5 Instruct** checkpoints in **safetensors `float32`**:

1. `Qwen/Qwen2.5-1.5B-Instruct`
2. `Qwen/Qwen2.5-0.5B-Instruct` (fallback if 1.5B does not fit)

These are ungated (Apache 2.0) and fit ~16 GB runners. They are **not** strong code-review agents: tool use is unreliable, multi-step OCR loops often stall, and review quality is limited compared with larger or hosted models.

**Why these sizes.** MAX 26.4 on CPU rejects GGUF/`q4_k` at startup (`quantization_encoding of 'q4_k' not supported`). A 7B `float32` model needs ~28 GB and OOMs on typical hosted runners. Until Modular ships better CPU encodings (or larger runners / `llm_url`), the chain stays on 0.5B–1.5B.

**Tool-calling proxy.** OCR expects OpenAI-style `tool_calls`. Qwen2.5 on local MAX usually emits Hermes-style `<tool_call>…</tool_call>` (or plain text) instead. When the served model ID matches Qwen 2.5, [`scripts/qwen25-max-tool-call-proxy.py`](scripts/qwen25-max-tool-call-proxy.py) starts automatically (OCR → `:8001` proxy → MAX `:8000`). It injects a short tool-use policy, promotes near-miss tool text into structured calls, rewrites bad paths, and short-circuits OCR’s weak review filter so tiny models do not veto real findings into a false LGTM. External `llm_url` endpoints skip the proxy. Outcome gates still fail the job on incomplete reviews (e.g. `review_item_failed`, tool errors with no comments) instead of posting a false LGTM.

**Roadmap.** When stronger models become usable on CPU under MAX (quantization, larger hosted runners, or better open weights in the chain), we will promote them in [`config/models.cpu.json`](config/models.cpu.json) and shrink or drop Qwen2.5-specific proxy workarounds. For production-quality reviews today, prefer `llm_url` with a tool-capable hosted model.

## Pinned versions

All defaults live in [`config/versions.json`](config/versions.json):

| Component | Version | Install |
|-----------|---------|---------|
| OCR | 1.7.4 | npm |
| codebase-memory-mcp | 0.8.1 | npm |
| Modular MAX | 26.4.0 | pip |
| Node.js | 24 | setup-node |
| Runner | ubuntu-24.04 | — |
| Python | 3.12 | setup-python |

### Model fallback chain

The local CPU path uses an **ordered fallback chain** (see `candidates` in [`config/models.cpu.json`](config/models.cpu.json)). [`select-model.sh`](scripts/select-model.sh) drops candidates that are gated or don't fit RAM; [`setup-modular-max`](actions/setup-modular-max/action.yml) tries each survivor and serves the first that loads. The served model is shown in the job summary.

**MAX 26.4 on CPU (current pin).** Only **safetensors `float32`** models load reliably. Passing `--quantization-encoding q4_k` (GGUF) is rejected at startup:

```text
quantization_encoding of 'q4_k' not supported by MAX engine
```

Default chain and limitations (Qwen2.5 1.5B → 0.5B, tool-call proxy, roadmap): see [Default Qwen2.5 CPU models](#default-qwen25-cpu-models).

**Latency.** CPU `float32` 1.5B reviews take minutes per file. The warm workflow on `main` pre-compiles models so PR jobs restore cache instead of cold-starting.

**RAM pre-flight.** Peak RAM is estimated from parameter count × encoding bytes + safety factor (see `ram_estimate` in config). Candidates that exceed `MemAvailable` are dropped.

**Debugging OCR.** Set `debug_review: true` to enable telemetry content logging, print a session trace in the job log, and upload the JSONL audit as a workflow artifact (`ocr-session-<id>`). Locally: `ocr session list` / `ocr session show <id>`. On CI the audit lives only on the ephemeral runner unless uploaded — previous runs without `debug_review` left no retrievable artifact.

## Security

This workflow uses `pull_request_target` so secrets and cache are available for fork PRs. OCR only **reads git diffs** and does not execute code from the PR branch. See [GitHub's guidance](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#pull_request_target) on `pull_request_target` risks.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| OOM / runner killed | Workflow auto-downgrades to smaller models; reduce `max_estimated_tokens` or set `model_override` to a smaller model |
| Slow first run | Run the index warmer and [MAX cache warmer](#max-model-cache) on `push` to `main` |
| Cache miss on PR | Ensure index workflow ran on the current base branch SHA |
| Review skipped | PR diff estimate exceeded `max_estimated_tokens` |
| `max serve` timeout | First CPU cold start can take 20+ minutes (download + compile). Check job log for `tail /tmp/max-serve.log`. Run the MAX cache warmer on `main` so later PRs restore caches. |
| LLM `context deadline exceeded` | OCR’s default per-request HTTP timeout is **300s**. Local CPU Qwen often needs longer; the workflow sets `OCR_LLM_TIMEOUT=900` and `ocr review --timeout 25` (minutes per file) when using local MAX. (`llm.timeout_sec` is not a valid `ocr config set` key on OCR 1.7.x.) |

## MAX model cache

MAX downloads weights from Hugging Face and compiles them for your device. Both are cached via GitHub Actions `actions/cache`:

| Cache path | Contents |
|------------|----------|
| `~/.venv-max` | Python virtualenv with Modular MAX installed (skips `pip install` on a hit; keyed on exact Python patch + MAX version) |
| `~/.cache/huggingface` | Downloaded model weights (safetensors) |
| `$RUNNER_TOOL_CACHE/max-gguf` | Downloaded GGUF weight files (`--weight-path`) |
| `$RUNNER_TOOL_CACHE/modular-max-cache` | MAX compile cache / MEF (`MODULAR_MAX_CACHE_DIR`) |

One combined cache holds artifacts for the **whole candidate chain**, keyed on a hash of [`config/models.cpu.json`](config/models.cpu.json) + MAX version (so editing the chain busts it, and PRs share a prefix `restore-key`). The warm workflow (on push to `main`) is what populates it; PR runs restore it read-only. Caches are only saved on success, so a failed compile never poisons the key.

Caching is optional: set `cache_models: false` to disable it, or use `llm_url` to point at an external OpenAI-compatible API — in that case local MAX, its caches, and the RAM pre-flight are all skipped, and `model_override` names the model sent to the external API.

The `setup-modular-max` action serves the first working candidate (compiling on first load, which persists to `MODULAR_MAX_CACHE_DIR`), saves caches on success, and waits up to 30 minutes per candidate for the health endpoint on cold start.

**Recommended:** add the MAX cache warmer (copy [`examples/consumer-warm-max-workflow.yml`](examples/consumer-warm-max-workflow.yml) to `.github/workflows/llm-warm-max.yml`) so **pushes to `main`** pre-compile every model in the chain. PR jobs restore that cache read-only (`cache write denied` on `pull_request_target` is expected). After changing `models.cpu.json` or the cache key (`-v3`), merge to `main` once so the warm job populates the new key before expecting fast PR reviews.

**Alternatives:** use `llm_url` to point at an external API and skip local MAX; or run [`modular/max-full`](https://docs.modular.com/max/container/) in Docker with the same volume mounts (GPU-oriented, but supports `--devices cpu`).


## Repository layout

```text
actions/           Composite actions (setup-ocr, setup-cbm, setup-max, post-comments)
scripts/           Shell helpers (estimate tokens, select model, cache index)
config/            Pinned versions and model tiers
.github/workflows/ Reusable workflows (review-pr, index-base-branch, warm-max-model)
examples/          Copy-paste consumer workflows
```

## License

MIT — see [LICENSE](LICENSE).
