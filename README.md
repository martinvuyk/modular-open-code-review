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

| Input | Default | Description |
|-------|---------|-------------|
| `ocr_version` | `1.7.4` | OCR npm version |
| `cbm_version` | `0.8.1` | codebase-memory-mcp npm version |
| `modular_version` | `26.4.0` | Modular pip package |
| `runner` | `ubuntu-24.04` | Runner label |
| `model_override` | `""` | Force a Hugging Face model ID |
| `max_estimated_tokens` | `500000` | Skip review when preflight estimate exceeds this |
| `post_comments` | `true` | Post GitHub review comments |
| `llm_url` | `""` | External OpenAI-compatible API (skips local MAX, its caching, and RAM gating). Set `model_override` to name the external model. |
| `llm_extra_body` | `""` | JSON merged into every LLM request. Only for thinking-capable models, e.g. `{"chat_template_kwargs": {"enable_thinking": false}}`. Leave empty for Qwen2.5. |
| `cache_models` | `true` | Cache the MAX venv + weights/compile artifacts (local MAX only; no effect with `llm_url`) |
| `action_ref` | `main` | Ref of this repo for scripts |

### `max_estimated_tokens`

This is a **preflight safety cap**, not an OCR or API billing limit. Before loading the LLM, we estimate prompt size from the git diff plus per-file/bundle overhead (see [`scripts/estimate-review-tokens.sh`](scripts/estimate-review-tokens.sh)). The default matches `max_tokens` in [`config/models.cpu.json`](config/models.cpu.json) and is meant to avoid OOM/timeouts on 16 GB GitHub-hosted runners.

Rough scale (heuristic, not exact):

| PR shape | Approx. estimate |
|----------|------------------|
| ~500 changed lines, few files | ~50k–80k |
| ~5k changed lines, ~20 files | ~150k–250k |
| ~50+ changed files (even modest diffs) | ~400k+ (per-file overhead dominates) |

Example with overrides:

```yaml
jobs:
  review:
    uses: martinvuyk/modular-open-code-review/.github/workflows/review-pr.yml@main
    with:
      model_override: Qwen/Qwen2.5-7B-Instruct
      max_estimated_tokens: '300000'
    secrets: inherit
```

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

Default LLM tiers (see [`config/models.cpu.json`](config/models.cpu.json)):

| Tier | Model |
|------|-------|
| Small PRs | `Qwen/Qwen2.5-0.5B-Instruct` (`float32` on CPU, ~2 GB RAM) |
| Medium/large | `Qwen/Qwen2.5-1.5B-Instruct` (`float32`, ~6 GB RAM), lower concurrency |

These sizes are chosen so the `float32` weights fit comfortably in the 16 GB of a standard runner. Larger Qwen sizes at `float32` are much heavier (3B ≈ 12–14 GB, 7B ≈ 28 GB) and OOM-kill during compilation on 16 GB runners; override the model only if you run on a larger runner.

Before starting MAX, [`select-model.sh`](scripts/select-model.sh) pre-flights the memory fit: it reads the model's exact parameter count from the Hugging Face API (falling back to parsing the size out of the model id), multiplies by the bytes-per-weight of the target encoding, and applies `ram_estimate.safety_factor` + `ram_estimate.base_overhead_gb` from [`config/models.cpu.json`](config/models.cpu.json). If the chosen tier does not fit `MemAvailable`, it downgrades to a smaller tier; if even the smallest does not fit, it skips the review with a clear reason instead of letting MAX OOM mid-compile. The estimate (needed vs. free) is printed in the job summary.

The default `0.5B`, `1.5B`, and `7B` Qwen2.5 instruct models are Apache 2.0 and need no Hugging Face license acceptance flow. Note that `Qwen/Qwen2.5-3B-Instruct` is **not** Apache 2.0 (Qwen Research License, non-commercial), so it is intentionally not a default.

## Secrets

The default Qwen models do not require secrets. Add repository secrets when you override the model or use gated Hugging Face weights.

| Secret | Required | Description |
|--------|----------|-------------|
| `HF_TOKEN` | Optional | [Hugging Face access token](https://huggingface.co/settings/tokens). Used when downloading models from the Hub (gated models, higher rate limits). Passed via `secrets: inherit` in the consumer workflow. |

Example — set `HF_TOKEN` in your repo settings, then keep `secrets: inherit` in the workflow (already in the quick-start example). The reusable workflow forwards it to `max serve` when starting the local LLM.


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

## MAX model cache

MAX downloads weights from Hugging Face and compiles them for your device. Both are cached via GitHub Actions `actions/cache`:

| Cache path | Contents |
|------------|----------|
| `~/.venv-max` | Python virtualenv with Modular MAX installed (skips `pip install` on a hit; keyed on exact Python patch + MAX version) |
| `~/.cache/huggingface` | Downloaded model weights |
| `$RUNNER_TOOL_CACHE/modular-max-cache` | MAX compile cache / MEF (`MODULAR_MAX_CACHE_DIR`) |

The warm workflow (on push to `main`) is what populates these caches; PR runs restore them read-only. Caches are only saved on success, so a failed compile never poisons the key.

Caching is optional: set `cache_models: false` to disable it, or use `llm_url` to point at an external OpenAI-compatible API — in that case local MAX, its caches, and the RAM pre-flight are all skipped, and `model_override` names the model sent to the external API.

The `setup-modular-max` action runs [`max warm-cache`](https://docs.modular.com/max/cli/warm-cache/) before `max serve`, saves caches even when a job fails after download, and waits up to 30 minutes for the health endpoint on cold start.

**Recommended:** add the MAX cache warmer (copy [`examples/consumer-warm-max-workflow.yml`](examples/consumer-warm-max-workflow.yml) to `.github/workflows/llm-warm-max.yml`) so pushes to `main` pre-download and compile the tier models from [`config/models.cpu.json`](config/models.cpu.json). PR reviews then restore that cache instead of starting cold.

**Alternatives:** use `llm_url` to point at an external API and skip local MAX; or run [`modular/max-full`](https://docs.modular.com/max/container/) in Docker with the same volume mounts (GPU-oriented, but supports `--devices cpu`).

### Why we compile instead of downloading a prebuilt model

There is no downloadable "prebuilt binary" for a chosen model. MAX's compiled artifact is a **MEF** (Modular Executable Format) file stored in `MODULAR_MAX_CACHE_DIR`. Per [Modular](https://forum.modular.com/t/how-to-import-and-run-an-exported-max-model-from-mef/580), the serialized MEF cache is **device- and MAX-version-specific and not portable**, so Modular does not publish per-model compiled artifacts to download.

Our "download once, fall back to compile" strategy is therefore implemented with the GitHub Actions cache itself: `max warm-cache` compiles the model once, we persist `MODULAR_MAX_CACHE_DIR`, and later runs restore it (cache hit = fast path, cache miss = recompile). This is the supported equivalent of shipping a prebuilt binary within a single runner OS + MAX version.

A manual MEF export/import path also exists but Modular considers it "largely obsolete" now that automatic graph caching is reliable, so we do **not** use it. Kept here for reference only:

```python
# DISABLED / reference only — MEF is not portable across devices or MAX versions.
# from max.engine import InferenceSession
# session = InferenceSession()
# compiled = session.compile(model_path)   # produce CompiledModel
# compiled.export_mef("qwen-cpu.mef")       # serialize compiled artifact
# session.load("qwen-cpu.mef")              # later: load without recompiling
```


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
