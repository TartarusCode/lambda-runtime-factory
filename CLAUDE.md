# CLAUDE.md — lambda-runtime-factory

## Project

Monorepo of AWS Lambda **layer** packages for `provided.al2023`. Each runtime under `runtimes/<id>/` has `runtime.json`, bootstrap, helpers, checksums, and SAM examples. CI matrix is driven by `tools/runtime_lib/runtime_manifest.py list`.

## Runtime families

Family defaults live in `tools/runtime_lib/runtime_manifest.py` (`RUNTIME_FAMILY_DEFAULTS`). Version bumps: `python3 tools/runtime_lib/bump_version.py bump <runtime> <version>` or weekly `check` / `bump-latest` workflow.

## Version lines (bun / deno / go / rust)

- Runtimes track a `version_line` (major.minor) in `runtime.json`; ids are `<framework><major><minor>` (e.g. `go126` = line `1.26`).
- Same-line patch bumps happen in place; **new minor lines create a new runtime dir** instead of mutating an existing one.
- `bump_version.py`: `LINE_FAMILIES` maps family → id prefix; `check_latest_*(version_line)` filter to a line; `detect_new_lines()` finds untracked upstream lines; `add_runtime_line(family, version)` clones the newest same-family dir and rewrites `runtime.json` + example files + checksums.
- `PYTHON_LINE_FAMILIES` (`portable-pypy`) are keyed on the **Python** version, not the PyPy version: `pypy311` = PyPy on Python 3.11, `check_latest_pypy(python_version)` filters to that line, and `detect_new_lines()` proposes a new `pypyNNN` when upstream adds a Python line **newer than the newest tracked one** (never back-fills 2.7/3.6–3.10). GraalPy is per-Python in its id already and has no line scheme.
- **Roll-off** bounds the matrix: `roll-off [--keep N] [--apply] [--prune]` (default keep 2, report-only) marks runtimes on lines older than the newest N per family with `"deprecated": true`. `manifest_matrix()` excludes them, `check_updates()` skips them unless named, and `--prune` (requires `--apply`) deletes their directories — refusing ever to empty a family.
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

- **Archive filename is dynamic**: PyPy changed its archive extension (`.tar.bz2` → `.tar.gz` at 8.0.0), so the `portable-pypy` family templates use `{archive_ext}` and `bump_version.py` reads the real filename from `versions.json` (via `ARCHIVE_NAME_RESOLVERS`). Bumps persist the resolved value as `archive_ext` in `runtime.json`; the family default is `.tar.bz2` so existing runtimes are unchanged.
- **Latest selection** is by numeric `pypy_version` (not `versions.json` order) and filters on `stable` + the Python line (`python_version` in the manifest, else parsed from `pypy3.11-v…`).
- PyPy publishes no checksum index, so the archive is downloaded once and hashed (`_fetch_checksum_pypy`).
- A new Python line is a **new runtime** (`pypy312`), auto-added by `bump-latest`; same-line PyPy bumps happen in place.

## Fault tolerance

- `bump-latest` isolates each runtime: a failure is printed, collected, and skipped rather than aborting the run. `bump_latest_all` returns `[(runtime_id, message), …]`; `main` exits non-zero when non-empty and writes them to `--failures-json <path>` when asked.
- All upstream writes happen only after checksums resolve — no partial `runtime.json`/checksum writes, and `add_runtime_line` removes a half-created directory on failure.
- `tools/bin/build-runtime` retries archive downloads with `--retry-all-errors` (HTTP/2 stream resets otherwise abort a build), and falls back to the archive's single top-level directory when `archive_root_dir` does not match what upstream extracted.
- `check-updates.yml` runs the bump with `continue-on-error`, adds a **Skipped Runtimes** section to the PR body, then fails the job so skips are visible.
- GitHub API calls (bun/deno/graalpy) authenticate with `GITHUB_TOKEN`/`GH_TOKEN` and paginate; `check-updates.yml` exports `GITHUB_TOKEN` to the job.

## GraalPy (`graalpy312`, `graalpy313`, family `graalpy`)

- **Python version in assets**: Release archives embed the Python version in the name — `graalpy3.12-{ver}-{arch}.tar.gz` (3.12), `graalpy3.13-{ver}-{arch}.tar.gz` (3.13). Pre-25.1 releases used `graalpy-{ver}-{arch}.tar.gz` (Python 3.12). The 3.12 line ends at `25.2.4`; 3.13 is separate.
- **Manifest**: each runtime stores its `python_version` (e.g. `"3.13"`); the `graalpy` family uses `{python_version}` in `archive_name`, `archive_url`, `archive_root_dir`, `checksum_name`, `package_name`, and `helper_install_dir` (`graalpy/lib/python{python_version}/site-packages`).
- **Three different version identifiers**: the release **tag**, the **asset filename** version, and the **extracted directory** version can all differ. e.g. tag `graal-25.3.4` → asset `graalpy3.13-25.3.4.1-linux-amd64.tar.gz`; tag `graal-25.4.4` → asset `graalpy3.13-25.4.4-linux-amd64.tar.gz` which extracts to `graalpy3.13-25.4.4.1.1-linux-amd64`.
  - `distribution_version` is the **asset** version. `bump_version.py` derives it from asset names (`_graalpy_asset_version`), not the tag.
  - `archive_url` uses `{release_tag}` (defaults to `distribution_version`); a bump records `release_tag` in `runtime.json` only when it differs, and clears a stale one.
  - `archive_root_dir` cannot be known from the index; `tools/bin/build-runtime` falls back to the single top-level directory when the manifest's name is absent, and fails loudly if the archive has more than one.
- **Bumping**: `bump_version.py` resolves archive/checksum names with the runtime's `python_version`. `check-latest-graalpy(python_version)` lists GitHub releases (`/releases?per_page=100`) and filters to assets matching that Python version, so a 3.12 runtime never bumps to a 3.13 release.
- **Bootstrap**: both runtimes set `/opt/graalpy/lib/python{python_version}/site-packages` on `sys.path`.

## Test infrastructure

- `tools/runtime_lib/tests/` holds pytest behavior tests (imports via `conftest.py` sys.path shim). Run with `make test` / `python3 -m pytest tools/runtime_lib/tests -q`.
- `ci.yml` `repo-checks` installs `pytest pytest-mock` and runs the suite.

## CI / audit

- **Workflow layout** (`ci.yml`): `repo-checks` runs validate/bash-n/check once; `grype-db` primes and caches the vulnerability database once; `runtime-checks` matrix (needs all three) builds and audits per runtime×arch. Release workflow mirrors download + Grype caches only.
- **Grype**: `bash tools/bin/audit-runtime` with `--fail-on high --only-fixed` — pin upstream when CVEs have fixed releases (e.g. Go 1.26.3 → 1.26.4). CI/release install `v0.98.0` to `${RUNNER_TEMP}/grype/bin` with Actions cache on binary and `~/.cache/grype`. **Measure first**: the database is ~2.8GB and a cold `grype db update` takes ~58s, while the scan itself is ~0.6s and a warm cache restore is ~10s. The `grype-db` job primes and caches the database once so the 21 matrix cells share it; its key is week-stamped and deliberately **excludes** `GRYPE_VERSION`, since the database is independent of the binary and a tool bump must not invalidate 2.8GB of cache.
- **Shell syntax**: `repo-checks` runs `bash -n` over every file in `tools/bin/`, which are extension-less — a `*.sh` glob matches only `_common.sh` and silently skips the rest.
- **SAM in CI**: x86 matrix cells with local invoke install from `requirements-ci.txt`, warm Docker image, then `make local-build` / `local-invoke`.
- **Artifacts**: GitHub Actions zips only on push to `main` (7-day retention); PRs rely on CI logs, not stored artifacts.
- Builds require Linux/WSL (`bash`, `curl`, `zip`, `unzip`, `make`).

## Local SAM

`make local-build` / `make local-invoke RUNTIME=<id>` — prefer repo on WSL filesystem, not `/mnt/c/...`.
