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

- **Workflow layout**: `ci.yml` — `repo-checks` runs validate/bash-n/check/URL-check once; `grype-db` refreshes and caches the vulnerability database once; `runtime-checks` matrix (needs all three) builds every runtime×arch and audits the ones whose payload changed. `release-runtime.yml` publishes, reusing the same `matrix` generator and composite action, and has its own `grype-db` primer that `publish` waits for. `audit-runtimes.yml` runs the same matrix weekly to catch CVE-database drift and primes the database first — it is usually the first run of a new ISO week, so usually the run that rolls the week's database entry. `check-updates.yml` bumps versions weekly.
- **Audit gating**: a Grype scan runs only when the cell's pinned archive changed. Findings come solely from the upstream payload (our `helpers/` ship no dependency metadata — no `.dist-info`/lockfiles), so re-scanning an unchanged archive can only report database drift that no commit caused, which would fail unrelated PRs. `runtime-build-env` uses the existing download-cache key as the change signal — `cache-hit == 'true'` means the bytes are identical, so it skips the 2.8GB database restore *and* the scan; a cold cache audits conservatively. The input is `grype: when-changed | always` and the action exports `audit` and `package-cache-hit` outputs that the workflows' steps are gated on. Audit-critical jobs pass `always`; `test_audit_gating_contract`-style tests in `test_runtime_lifecycle.py` enforce the default stays cheap and those jobs stay explicit.
- **Shared build environment**: `.github/actions/runtime-build-env` owns the download cache, the built-package cache, the Grype binary cache, the week-stamped database cache and the Grype install, so the CI matrix, the release matrix and the primers cannot drift apart (they did: the release workflow kept the old `grype-db-<os>-<GRYPE_VERSION>` key and cold-downloaded 2.8GB per cell). Pass `runtime`/`arch` to cache a runtime's archive and package; omit them for a database primer. `grype` and `db-refresh` are declared explicitly at every call site.
- **Runner labels are pinned, never `ubuntu-latest`**: the label migrates OS releases on GitHub's schedule (24.04 → 26.04 rolls out 2026-10-19 to 2026-11-19), silently changing the toolchain under native builds. `ARCH_RUNNERS` in `runtime_manifest.py` is the single source, used by both workflows; `test_runner_labels_are_pinned_not_floating` guards it. Bump deliberately and re-validate the build.
- **`paths-ignore: ['**.md']`** on ci.yml keeps docs-only changes from running the 22-cell matrix. `main` has no branch protection or required checks, so a skipped workflow cannot block a merge — re-check that before adding protection.
- **`repo-checks` runs for dependabot PRs** (it is ~10s and is the only validation an action bump gets); only the expensive matrix is skipped.
- **Every job has `timeout-minutes`** (and the build step 20) so a pathological upstream download fails fast instead of hanging a cell.
- **Release**: `release-runtime.yml` has a `concurrency` group with `cancel-in-progress: false` (publishes queue rather than get killed mid-flight) and gates `publish` on the `release` environment. The environment exists but has **no protection rules** — add required reviewers under Settings → Environments → release to make it a real approval gate.
- **Grype**: `bash tools/bin/audit-runtime` with `--fail-on high --only-fixed` — pin upstream when CVEs have fixed releases (e.g. Go 1.26.3 → 1.26.4). CI/release install `v0.98.0` to `${RUNNER_TEMP}/grype/bin` with Actions cache on binary and `~/.cache/grype`. **Measure first**: the database is ~2.8GB and a cold `grype db update` takes ~58s, while the scan itself is ~0.6s and a warm cache restore is ~10s. The `grype-db` job refreshes and caches the database once so the 21 matrix cells share it; its key is week-stamped and deliberately **excludes** `GRYPE_VERSION`, since the database is independent of the binary and a tool bump must not invalidate 2.8GB of cache.
- **One writer per database key**: Actions cache entries are immutable — a restore whose key already exists is never saved back (`Cache hit occurred on the primary key ..., not saving cache`). So `runtime-build-env` only *restores* the database (`actions/cache/restore`) and the single `db-refresh: "true"` call site per workflow (`grype-db` in `ci.yml`, `audit-runtimes.yml`, `release-runtime.yml`) is the only thing that runs `grype db update` and saves the result under the week's key. It skips the refresh when the week's entry exists (the update could only be discarded at job end) and skips the save when the refresh failed — otherwise the first job of a new week would freeze the *previous* week's database under the new key and every later refresh in that week would be thrown away. `test_runtime_lifecycle.py` enforces the contract.
- **Built-package cache**: `runtime-build-env` caches `…/<runtime>/<arch>/artifacts` under a key hashed over `runtime.json`, `checksums/**`, `bootstrap/**`, `helpers/**`, `tools/bin/build-runtime` and `runtime_manifest.py` — deliberately **no `restore-keys`**, since a package built from other inputs must never be restored. A hit means every build input is unchanged, so the CI cell skips its build step (`package-cache-hit`) instead of repeating the same extraction and zip (up to ~80s for rust) and the restored zip still feeds the SAM example and the artifact upload. The packages are not tiny — rust is ~0.5–0.6GB per arch, the rest 37–110MB — so the entries are read every run (hot, self-correcting under LRU) but hold ≈4GB of the repo's 10GB cache budget. `release-runtime` keeps an unconditional build: a publish must come from a fresh build.
- **Shell syntax**: `repo-checks` runs `bash -n` over every file in `tools/bin/`, which are extension-less — a `*.sh` glob matches only `_common.sh` and silently skips the rest.
- **Archive URLs**: `repo-checks` also runs `runtime_manifest.py validate --online`, which HEADs every active runtime's `archive_url` and fails only on 404/410. Derived fields such as `release_tag` can go stale from a merge (e.g. a merge kept `distribution_version: 25.4.4` next to `release_tag: "25.3.4"`, resolving to a 404 URL); this catches that in seconds instead of after a full build matrix.
- **SAM in CI**: x86 matrix cells with local invoke install from `requirements-ci.txt` (aws-sam-cli pinned there), warm the Docker image, then `make local-build` / `make local-invoke`. The warm step must pull `public.ecr.aws/sam/build-provided.al2023:latest-<arch>` — SAM resolves the arch-suffixed tag, and the bare `:latest` list resolves to a *different* image (1 of 15 layers shared), so warming it fetches ~734MB SAM never uses and leaves SAM to fetch ~695MB of its own inside the build step. `local-invoke-runtime` is the verification: it checks the `sam local invoke` exit status and requires a `200` response carrying the handler payload (every example returns `{"message": "hello world"}`), rather than only grepping for failure strings, and it skips `sam build --use-container` when `make local-build` already produced the build output. Measured per cell on a 7-cell x86 run: build 26–41s, invoke 11–25s, warm ~17s, `pip install aws-sam-cli` ~22s. Arm cells skip all of it behind `matrix.arch == 'x86_64'`; SAM does publish arm64 tags (`latest-arm64`) and the arm runner image ships Docker, but enabling it also needs `Architectures: [<arch>]` rendered into both local templates, since SAM otherwise defaults to x86_64.
- **Artifacts**: GitHub Actions zips only on push to `main` (7-day retention); PRs rely on CI logs, not stored artifacts.
- Builds require Linux/WSL (`bash`, `curl`, `zip`, `unzip`, `make`).

## Local SAM

`make local-build` / `make local-invoke RUNTIME=<id>` — prefer repo on WSL filesystem, not `/mnt/c/...`. Both reuse what is already there instead of rebuilding: `local-build` only rebuilds when a bootstrap/helper file is newer than the package, `local-invoke` only when the SAM build output is missing.
