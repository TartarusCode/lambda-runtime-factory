# lambda-runtime-monorepo

A monorepo for AWS Lambda custom runtimes, with PyPy as the first implemented runtime.

## Overview

The repository is now organized around runtime packages under `runtimes/` and shared tooling under `tools/`.

- Runtime-specific code, checksums, examples, and release metadata live under `runtimes/<runtime-id>/`
- Shared build, audit, publish, and local-test entrypoints live under `tools/bin/`
- Runtime metadata is declared in `runtimes/<runtime-id>/runtime.json`
- GitHub Actions uses the runtime manifest list as the source of truth for CI and release matrices

## Repository Layout

```text
runtimes/
  pypy311/
    runtime.json
    bootstrap/
    helpers/
    checksums/
    examples/
tools/
  bin/
  runtime_lib/
.github/workflows/
```

## Supported Runtimes

Implemented runtimes:

- `pypy311` — PyPy 3.11 (portable Linux builds from python.org)
- `graalpy312` — GraalPy 3.12
- `graalpy313` — GraalPy 3.13
- `bun13` — Bun 1.3.x
- `bun14` — Bun 1.4.x
- `deno29` — Deno 2.9.x (`provided.al2023` layer; bootstrap runs `deno` with sandbox flags for the Runtime API)
- `go126` — Go 1.26.x toolchain layer
- `go127` — Go 1.27.x toolchain layer
- `rust194` — Rust 1.96.x musl toolchain layer
- `rust198` — Rust 1.98.x musl toolchain layer

Runtimes for line-based frameworks (bun/deno/go/rust) are named `<framework><major><minor>` and each tracks a single `version_line` (e.g. `go126` tracks `1.26`). Minor-line updates create a new runtime rather than mutating an existing one; patch updates stay on the same runtime.

The shared tooling is intentionally runtime-agnostic so additional runtimes can be added without reworking the root build and release flow.

## Runtime Manifest

Each runtime package declares its build and release contract in `runtime.json`, including:

- runtime family
- runtime version or distribution identifier
- any overrides to the family defaults

Most runtime details are now derived from runtime-family defaults in `tools/runtime_lib/runtime_manifest.py`. For runtimes that follow an established family layout, the manifest can stay very small.

Use the manifest tooling from the repo root:

```bash
make list-runtimes
python3 tools/runtime_lib/runtime_manifest.py validate
```

## Common Commands

Supported build environment:

- Linux or WSL is required for build and release commands
- The shared tooling assumes native Linux tools such as `bash`, `tar`, `zip`, `unzip`, `curl`, `sha256sum`, and `make`
- Transient build work is staged under `${BUILD_ROOT:-$RUNNER_TEMP}` or `/tmp` to avoid slow cross-OS archive operations
  - release and CI artifacts are built from temp storage by default
  - set `EXPORT_ARTIFACT_DIR=/some/path` if you want a copy of the final zip preserved outside temp storage

Build a specific runtime:

```bash
make build RUNTIME=pypy311
make build RUNTIME=deno29
```

Audit a built runtime:

```bash
make audit RUNTIME=pypy311
```

Upload and publish a runtime layer:

```bash
make upload RUNTIME=pypy311
make publish RUNTIME=pypy311
```

Publish and publicize a runtime layer:

```bash
make publicize RUNTIME=pypy311
```

List the latest published layer versions:

```bash
make latest RUNTIME=pypy311
```

## Local SAM Test Flow

Each runtime can carry its own local SAM assets. For PyPy they live under:

- `runtimes/pypy311/examples/sam/template.local.example.yaml`
- `runtimes/pypy311/examples/sam/events/hello.json`
- `runtimes/pypy311/examples/sam/hello/Makefile`

Run the local smoke test from the repo root:

```bash
make local-build RUNTIME=pypy311
make local-invoke RUNTIME=pypy311
```

This flow:

- builds the runtime package under temp storage
- expands the local layer under temp storage
- renders a temp SAM template with resolved local paths
- runs `sam build --use-container` with a temp SAM build directory
- assembles a temp invoke bundle for `sam local invoke`

Requirements:

- Linux or WSL is required for Docker-backed SAM workflows
- For best local SAM performance, keep the repo on the WSL filesystem instead of `/mnt/c/...`
- Docker must be available
- `sam` must be installed in the environment where you run the commands

## GitHub Actions

The repo includes:

- `.github/workflows/ci.yml`
  - `repo-checks` (once per workflow): manifest validation, shell syntax, Python compile per runtime
  - `runtime-checks` (matrix): build, Grype audit, local SAM smoke tests on x86 when enabled
  - caches: upstream runtime downloads, Grype binary (`v0.98.0`) and DB, pip for `requirements-ci.txt` (SAM jobs only)
  - SAM x86 jobs pre-pull `public.ecr.aws/sam/build-provided.al2023:latest` before `sam build --use-container`
  - workflow artifacts: built zips upload on **push to `main` only**, `retention-days: 7` (PRs do not upload)
  - superseded PR runs cancel via workflow concurrency
- `.github/workflows/release-runtime.yml`
  - manual runtime-scoped release flow
  - rebuild, audit, upload, and publish steps
  - same download and Grype caches as CI (no SAM)

The release workflow expects an AWS role secret named `AWS_RELEASE_ROLE_ARN`.

CI pins `aws-sam-cli` in `requirements-ci.txt` for reproducible SAM installs.

## Adding A New Runtime

1. Create a new runtime directory under `runtimes/<runtime-id>/`.
2. Add a `runtime.json` manifest with artifact, Lambda, release, and local test metadata.
3. Add the runtime bootstrap, helper package, checksum file, and examples under that directory.
4. Run:

```bash
python3 tools/runtime_lib/runtime_manifest.py validate --runtime <runtime-id>
bash tools/bin/check-runtime <runtime-id>
make build RUNTIME=<runtime-id>
```

5. Add or adapt runtime-specific examples under `runtimes/<runtime-id>/examples/`.
6. Add or update CI expectations if the runtime needs extra validation steps beyond the shared defaults.

## Auto-adding new version lines

For `bun`/`deno`/`go`/`rust`, when an upstream release lands on a version line newer than
any tracked runtime, the `check` command reports it in its `new_lines` output and
`bump-latest` automatically creates the new runtime directory:

```bash
python3 tools/runtime_lib/bump_version.py check --json
# {"outdated": {...}, "new_lines": {"go-toolchain": {"id": "go127", "line": "1.27", "version": "1.27.0"}}}

python3 tools/runtime_lib/bump_version.py bump-latest
```

The new runtime is cloned from the newest existing runtime of the same family, its
`runtime.json` is rewritten for the new `version_line`, and checksums are fetched from
upstream. The weekly `/check-runtime-updates` workflow opens a single PR covering both
same-line patch bumps and new-line additions.

## PyPy Notes

The first runtime package, `pypy311`, still ships the hardened Lambda Runtime API implementation and the helper package for:

- structured logging
- init hooks for Provisioned Concurrency style warm-up
- optional X-Ray helper utilities

## License

Apache 2.0
