# CLAUDE.md — lambda-runtime-factory

## Project

Monorepo of AWS Lambda **layer** packages for `provided.al2023`. Each runtime under `runtimes/<id>/` has `runtime.json`, bootstrap, helpers, checksums, and SAM examples. CI matrix is driven by `tools/runtime_lib/runtime_manifest.py list`.

## Runtime families

Family defaults live in `tools/runtime_lib/runtime_manifest.py` (`RUNTIME_FAMILY_DEFAULTS`). Version bumps: `python3 tools/runtime_lib/bump_version.py bump <runtime> <version>` or weekly `check` / `bump-latest` workflow.

## Deno (`deno28`, family `deno`)

- **Upstream**: GitHub `denoland/deno` release zips `deno-{arch}-unknown-linux-gnu.zip` (flat zip: single `deno` binary at root, not a directory like Bun).
- **Build**: `artifact.binary_name` in the `deno` family triggers flat-binary layout in `tools/bin/build-runtime` — binary lands at `deno/deno` under the layer so `deno/lib` can hold the helper.
- **Bootstrap**: `deno run --allow-net --allow-env --allow-read="${LAMBDA_TASK_ROOT},/opt/deno/lib"` then `runtime.ts` (Runtime API loop, same contract as Bun).
- **Handler import**: Deno requires a file extension; `runtime.ts` resolves `hello.handler` to `${LAMBDA_TASK_ROOT}/hello.ts` (Bun does not need this).
- **Runtime id**: `deno28` = Deno 2.8.x line; bump `distribution_version` without `v` prefix (URLs use `v{distribution_version}`).
- **Checksums**: per-arch `{archive}.zip.sha256sum` on the release page; fetcher in `bump_version.py` (`_fetch_checksum_deno`).

## PyPy

Official downloads from `downloads.python.org/pypy` (portable Linux builds; the `portable-pypy` family name is historical).

## CI / audit

- **Workflow layout** (`ci.yml`): `repo-checks` runs validate/bash-n/check once; `runtime-checks` matrix builds and audits per runtime×arch. Release workflow mirrors download + Grype caches only.
- **Grype**: `bash tools/bin/audit-runtime` with `--fail-on high --only-fixed` — pin upstream when CVEs have fixed releases (e.g. Go 1.26.3 → 1.26.4). CI/release install `v0.98.0` to `${RUNNER_TEMP}/grype/bin` with Actions cache on binary and `~/.cache/grype`.
- **SAM in CI**: x86 matrix cells with local invoke install from `requirements-ci.txt`, warm Docker image, then `make local-build` / `local-invoke`.
- **Artifacts**: GitHub Actions zips only on push to `main` (7-day retention); PRs rely on CI logs, not stored artifacts.
- Builds require Linux/WSL (`bash`, `curl`, `zip`, `unzip`, `make`).

## Local SAM

`make local-build` / `make local-invoke RUNTIME=<id>` — prefer repo on WSL filesystem, not `/mnt/c/...`.
