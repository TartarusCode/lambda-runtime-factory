# Developing lambda-runtime-factory

Building, testing, releasing and extending the runtime packages. For what the repository
publishes — layer names, regions, architectures and how to consume them — see the
[README](README.md).

## Build environment

- Linux or WSL is required for build and release commands
- The shared tooling assumes native Linux tools such as `bash`, `tar`, `zip`, `unzip`, `curl`,
  `sha256sum`, and `make`
- Transient build work is staged under `${BUILD_ROOT:-$RUNNER_TEMP}` or `/tmp` to avoid slow
  cross-OS archive operations
  - release and CI artifacts are built from temp storage by default
  - set `EXPORT_ARTIFACT_DIR=/some/path` if you want a copy of the final zip preserved outside
    temp storage

## Common commands

```bash
make list-runtimes                    # the ids the current matrix builds
make build RUNTIME=pypy311            # build the layer package
make audit RUNTIME=pypy311            # Grype scan of the built package
make upload RUNTIME=pypy311           # push the package to S3
make publish RUNTIME=pypy311          # create the Lambda layer version
make publicize RUNTIME=pypy311        # publish and grant public permissions
make latest RUNTIME=pypy311           # print the current layer version ARNs
make check RUNTIME=pypy311            # validate the manifest and compile its Python
make test                             # repo test suite (pytest)
```

`upload`/`publish`/`publicize`/`latest` need AWS credentials. Builds and scans do not.

## The runtime manifest

Each runtime package declares its build and release contract in `runtimes/<id>/runtime.json`:
the runtime family, its version or distribution identifier, and any override of the family
defaults. Most details are derived from `RUNTIME_FAMILY_DEFAULTS` in
`tools/runtime_lib/runtime_manifest.py`, so a runtime that follows an established family layout
keeps a very small manifest. Anything derived (layer name, regions, S3 prefix, paths, bootstrap
and helper locations) can be inspected with:

```bash
make list-runtimes
python3 tools/runtime_lib/runtime_manifest.py validate
python3 tools/runtime_lib/runtime_manifest.py env --runtime pypy311 --arch x86_64
```

The manifest list is the single source of truth for the CI and release matrices — both call
`runtime_manifest.py matrix` through the same composite action.

## Local SAM test flow

Each runtime carries its own local SAM assets, for example under `runtimes/pypy311/examples/sam/`:

- `template.local.example.yaml`
- `events/hello.json`
- `hello/Makefile`

```bash
make local-build RUNTIME=pypy311
make local-invoke RUNTIME=pypy311
```

This flow builds the runtime package under temp storage, expands the local layer, renders a temp
SAM template with resolved local paths, runs `sam build --use-container` with a temp build
directory, and assembles a temp invoke bundle for `sam local invoke`. `local-invoke` asserts the
handler actually answered (exit status and a `200` response carrying the handler payload), and
skips `sam build` when `local-build` already produced the output.

Requirements:

- Linux or WSL is required for Docker-backed SAM workflows
- For best local SAM performance, keep the repo on the WSL filesystem instead of `/mnt/c/...`
- Docker must be available, and `sam` installed in the environment running the commands

## CI

`.github/workflows/ci.yml` runs on every push to `main` and every PR (docs-only changes are
skipped):

- `repo-checks` (once): manifest validation, `bash -n` over `tools/bin/`, a compile check per
  runtime and the online archive-URL check, plus the test suite
- `grype-db` (once): refreshes and caches the ~2.8GB vulnerability database for the matrix
- `runtime-checks` (matrix): builds every runtime × architecture, scans the ones whose pinned
  payload changed, and on x86 runs the local SAM smoke test
- caches: upstream runtime downloads, the built package, the Grype binary and database
- artifacts: built zips upload on **push to `main` only**, `retention-days: 7`
- superseded PR runs cancel via workflow concurrency

Three composite actions in `.github/actions/` hold the whole per-cell procedure so the workflows
cannot drift apart:

- `runtime-matrix` — the build/publish matrix, generated from the manifest list
- `runtime-build-env` — the download, package, Grype-binary and Grype-database caches
- `build-and-audit-runtime` — archive tooling, `make build`, `tools/bin/audit-runtime`

`.github/workflows/audit-runtimes.yml` runs the same matrix weekly to catch CVE-database drift,
and `.github/workflows/check-updates.yml` opens the weekly version-bump PR.

CI pins `aws-sam-cli` in `requirements-ci.txt` for reproducible SAM installs.

## Cutting a release

`.github/workflows/release-runtime.yml` takes two entry points: a `release-YYYY.MM.DD` tag push
(add `-2`, `-3` for a second release the same day) or a manual dispatch for an ad-hoc or partial
republish.

```bash
git tag release-2026.11.05
git push origin release-2026.11.05
```

The workflow never creates the tag itself: a tag pushed with `GITHUB_TOKEN` does not trigger
workflows, so the tag is always the record of what was published.

A `preflight` job validates the tag, checks `AWS_RELEASE_ROLE_ARN` is set, prints the OIDC
subject it will present, requires `ci.yml` to have succeeded for the commit, and assumes the role
once — so a credential problem fails in one job instead of in every matrix cell.

The role is assumed over OIDC (`id-token: write`; there are no static AWS keys) and must trust
the subject `repo:<owner>/<repo>:environment:release`. The preflight prints that subject, so a
trust-policy mismatch is a one-line diagnosis rather than only "credentials could not be loaded".

To exercise the whole path without publishing anything — preflight, caches, build and scan all
run — dispatch it as a dry run:

```bash
gh workflow run release-runtime.yml -f runtime=pypy311 -f arch=x86_64 -f dry_run=true
```

Publishing is idempotent for unchanged bytes: the layer's own `Content.CodeSha256` decides
whether a version is created, so re-running a release whose package is already published reuses
that version instead of minting another. Set `PUBLISH_FORCE_NEW_VERSION=1` to publish
unconditionally.

## Adding a new runtime

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
6. Add or update CI expectations if the runtime needs extra validation beyond the shared defaults.

## Adding a new version line

For `bun`/`deno`/`go`/`rust`, when an upstream release lands on a version line newer than any
tracked runtime, the `check` command reports it in its `new_lines` output and `bump-latest`
creates the new runtime directory:

```bash
python3 tools/runtime_lib/bump_version.py check --json
# {"outdated": {...}, "new_lines": {"go-toolchain": {"id": "go127", "line": "1.27", "version": "1.27.0"}}}

python3 tools/runtime_lib/bump_version.py bump-latest
```

The new runtime is cloned from the newest existing runtime of the same family, its `runtime.json`
is rewritten for the new `version_line`, and checksums are fetched from upstream. The weekly
`check-runtime-updates` workflow opens a single PR covering both same-line patch bumps and
new-line additions.

PyPy works the same way, but its line is the **Python** version, so a new Python line is a new
runtime (`pypy311` → `pypy312`):

```bash
python3 tools/runtime_lib/bump_version.py check --json
# "new_lines": {"portable-pypy": {"id": "pypy312", "line": "3.12", "version": "pypy3.12-v8.0.0"}}
```

Only Python lines newer than the newest tracked one are proposed — upstream's older lines (2.7,
3.6–3.10) are never back-filled.

## Rolling off old lines

Without a bound the build matrix grows one cell per new line forever. `roll-off` keeps the newest
N lines per family and marks the rest deprecated:

```bash
make roll-off                              # report only (default: keep the newest 2 lines per family)
make roll-off ARGS="--keep 1"              # see what a tighter policy would roll off
make roll-off ARGS="--apply"               # write "deprecated": true
make roll-off ARGS="--apply --prune"       # also delete the rolled-off directories
```

Deprecated runtimes are dropped from the build matrix and from `check`/`bump-latest`, but their
files stay until `--prune` and they can still be built on demand with `make build RUNTIME=<id>`.
Pruning refuses to empty a whole family.

## Fault tolerance

`bump-latest` treats every runtime independently. If one runtime cannot be updated — an upstream
archive was renamed or removed, a transient network error, an unexpected tag format — that
runtime is skipped and reported, and the remaining runtimes are still bumped and committed:

```
pypy311: pypy3.11-v7.3.23 -> pypy3.11-v8.0.0
  !! Failed to bump pypy311: RuntimeError: GET ... failed: HTTP 404 Not Found
...
1 runtime(s) could not be updated:
  - pypy311: RuntimeError: GET https://downloads.python.org/pypy/pypy3.11-v8.0.0-linux64.tar.bz2 failed: HTTP 404 Not Found
```

The command exits non-zero when any runtime failed, so CI is red; the weekly workflow still opens
the PR for the runtimes that succeeded and lists the skipped ones under **Skipped Runtimes** in
the PR body. Pass `--failures-json <path>` to write a machine-readable report of the failures.

No partial state is written: a runtime's checksums are resolved from upstream *before* its
`runtime.json` or checksum file is touched, and a new runtime directory is removed again if any
step fails.

### Version source notes

- `bun` / `deno` / `graalpy` use the GitHub releases API, authenticated with
  `GITHUB_TOKEN`/`GH_TOKEN` when present (avoids the unauthenticated 60 req/h limit) and paginated
  so a tracked line's newest patch is not missed behind pre-release tags.
- `go` uses `go.dev/dl/?mode=json`; `rust` uses `channel-rust-stable.toml`.
- `pypy` uses `downloads.python.org/pypy/versions.json`. The newest release is chosen by numeric
  version (not list order), and the archive **filename** — including its extension — is read from
  that index, because PyPy has changed it between releases (`.tar.bz2` → `.tar.gz` at 8.0.0). The
  resolved extension is persisted as `archive_ext` in the runtime manifest so the build downloads
  the real upstream file. PyPy publishes no checksum index, so the archive is downloaded once and
  hashed.

## Runtime package notes

The PyPy packages (`pypy311`, `pypy312`) ship the hardened Lambda Runtime API implementation and
a helper package providing structured logging, init hooks for Provisioned Concurrency style
warm-up, and optional X-Ray helper utilities.
