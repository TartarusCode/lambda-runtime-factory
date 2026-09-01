# CLAUDE.md — lambda-runtime-factory

## Project

Monorepo of AWS Lambda **layer** packages for `provided.al2023`. Each runtime under `runtimes/<id>/` has `runtime.json`, bootstrap, helpers, checksums, and SAM examples. CI matrix is driven by `tools/runtime_lib/runtime_manifest.py list`.

## Runtime families

Family defaults live in `tools/runtime_lib/runtime_manifest.py` (`RUNTIME_FAMILY_DEFAULTS`). Version bumps: `python3 tools/runtime_lib/bump_version.py bump <runtime> <version>` or weekly `check` / `bump-latest` workflow.

## Version lines (bun / deno / go / rust)

- Runtimes track a `version_line` (major.minor) in `runtime.json`; ids are `<framework><major><minor>` (e.g. `go126` = line `1.26`).
- Same-line patch bumps happen in place; **new minor lines create a new runtime dir** instead of mutating an existing one.
- `bump_version.py`: `LINE_FAMILIES` maps family → id prefix; `check_latest_*(version_line)` filter to a line; `detect_new_lines()` finds untracked upstream lines; `add_runtime_line(family, version)` clones the newest same-family dir and rewrites `runtime.json` + example files + checksums.
- `check --json` emits `{"outdated": {...}, "new_lines": {...}}`; `bump-latest` bumps `outdated` then auto-adds `new_lines`. Weekly `check-updates.yml` opens one PR for both.
- `rust` family channel is TOML: only the `[pkg.rust]` section has the real version (arch/components sub-sections appear later and may contain `version = ""`).

## Deno (`deno29`, family `deno`)

- **Upstream**: GitHub `denoland/deno` release zips `deno-{arch}-unknown-linux-gnu.zip` (flat zip: single `deno` binary at root, not a directory like Bun).
- **Build**: `artifact.binary_name` in the `deno` family triggers flat-binary layout in `tools/bin/build-runtime` — binary lands at `deno/deno` under the layer so `deno/lib` can hold the helper.
- **Bootstrap**: `deno run --allow-net --allow-env --allow-read="${LAMBDA_TASK_ROOT},/opt/deno/lib"` then `runtime.ts` (Runtime API loop, same contract as Bun).
- **Handler import**: Deno requires a file extension; `runtime.ts` resolves `hello.handler` to `${LAMBDA_TASK_ROOT}/hello.ts` (Bun does not need this).
- **Runtime id**: `deno29` = Deno 2.9.x line; bump `distribution_version` without `v` prefix (URLs use `v{distribution_version}`).
- **Checksums**: per-arch `{archive}.zip.sha256sum` on the release page; fetcher in `bump_version.py` (`_fetch_checksum_deno`).

## PyPy

Official downloads from `downloads.python.org/pypy` (portable Linux builds; the `portable-pypy` family name is historical).

## GraalPy (`graalpy312`, `graalpy313`, family `graalpy`)

- **Python version in assets**: Release archives embed the Python version in the name — `graalpy3.12-{ver}-{arch}.tar.gz` (3.12), `graalpy3.13-{ver}-{arch}.tar.gz` (3.13). Pre-25.1 releases used `graalpy-{ver}-{arch}.tar.gz` (Python 3.12). The 3.12 line ends at `25.2.4`; 3.13 is separate.
- **Manifest**: each runtime stores its `python_version` (e.g. `"3.13"`); the `graalpy` family uses `{python_version}` in `archive_name`, `archive_url`, `archive_root_dir`, `checksum_name`, `package_name`, and `helper_install_dir` (`graalpy/lib/python{python_version}/site-packages`).
- **Bumping**: `bump_version.py` resolves archive/checksum names with the runtime's `python_version`. `check-latest-graalpy(python_version)` lists GitHub releases (`/releases?per_page=100`) and filters to assets matching that Python version, so a 3.12 runtime never bumps to a 3.13 release.
- **Bootstrap**: both runtimes set `/opt/graalpy/lib/python{python_version}/site-packages` on `sys.path`.

## Test infrastructure

- `tools/runtime_lib/tests/` holds pytest behavior tests (imports via `conftest.py` sys.path shim). Run with `make test` / `python3 -m pytest tools/runtime_lib/tests -q`.
- `ci.yml` `repo-checks` installs `pytest pytest-mock` and runs the suite.

## CI / audit

- **Workflow layout** (`ci.yml`): `repo-checks` runs validate/bash-n/check once; `runtime-checks` matrix builds and audits per runtime×arch. Release workflow mirrors download + Grype caches only.
- **Grype**: `bash tools/bin/audit-runtime` with `--fail-on high --only-fixed` — pin upstream when CVEs have fixed releases (e.g. Go 1.26.3 → 1.26.4). CI/release install `v0.98.0` to `${RUNNER_TEMP}/grype/bin` with Actions cache on binary and `~/.cache/grype`.
- **SAM in CI**: x86 matrix cells with local invoke install from `requirements-ci.txt`, warm Docker image, then `make local-build` / `local-invoke`.
- **Artifacts**: GitHub Actions zips only on push to `main` (7-day retention); PRs rely on CI logs, not stored artifacts.
- Builds require Linux/WSL (`bash`, `curl`, `zip`, `unzip`, `make`).

## Local SAM

`make local-build` / `make local-invoke RUNTIME=<id>` — prefer repo on WSL filesystem, not `/mnt/c/...`.
