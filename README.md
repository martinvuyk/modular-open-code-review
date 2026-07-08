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
| `llm_url` | `""` | External OpenAI-compatible API (skips local MAX) |
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
| Small PRs | `Qwen/Qwen2.5-3B-Instruct` |
| Medium/large | `Qwen/Qwen2.5-7B-Instruct` (when ≥14 GB RAM free) |

Qwen models use Apache 2.0 and do not require a Hugging Face license acceptance flow (unlike Meta Llama).

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
| `~/.cache/huggingface` | Downloaded model weights |
| `$RUNNER_TOOL_CACHE/modular-max-cache` | MAX compile cache (`MODULAR_MAX_CACHE_DIR`) |

The `setup-modular-max` action runs [`max warm-cache`](https://docs.modular.com/max/cli/warm-cache/) before `max serve`, saves caches even when a job fails after download, and waits up to 30 minutes for the health endpoint on cold start.

**Recommended:** add the MAX cache warmer (copy [`examples/consumer-warm-max-workflow.yml`](examples/consumer-warm-max-workflow.yml) to `.github/workflows/llm-warm-max.yml`) so pushes to `main` pre-download and compile the tier models from [`config/models.cpu.json`](config/models.cpu.json). PR reviews then restore that cache instead of starting cold.

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
