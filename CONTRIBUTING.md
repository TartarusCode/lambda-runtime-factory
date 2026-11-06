# Developing lambda-runtime-factory

Building, testing, releasing and extending the runtime packages. For what the repository
publishes — layer names, regions, architectures and how to consume them — see the
[README](README.md).

## Build environment

- Linux or WSL is required for build and release commands
- The shared tooling assumes native Linux tools such as `bash`, `tar`, `zip`, `unzip`, `curl`,
  `sha256sum`, `cc`, and `make`. `cc` links the Go/Rust bootstrap launcher; without it the
  shell bootstrap is kept (and the distroless image build refuses it)
- Docker with BuildKit (`docker buildx`) is required for the container artifact
- `readelf` (binutils) is used to resolve the payload's shared libraries
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

Deployment regions and the S3 bucket base name are repo-level settings in `REPO_DEFAULTS`, not
per-runtime fields. A runtime manifest that redeclares them silently wins over the global value,
because the manifest is merged last.

One field is neither derived nor a version: `layer_publishable` marks a runtime whose package
cannot be delivered as a Lambda layer, because a deployment package is capped at 250 MB
unzipped — the function plus every layer attached to it. It is still built and scanned
everywhere; only the publish matrix skips it. Because it is resolved from the *merged*
manifest, declaring it on a family in `RUNTIME_FAMILY_DEFAULTS` covers every line of that
family, including lines a version bump has not generated yet. See [DELIVERY.md](DELIVERY.md).

The same view carries the byte budget: `max_unzipped_bytes` (262144000) and
`warn_unzipped_bytes` (209715200) live in `REPO_DEFAULTS` and are exported to the build as
`MAX_UNZIPPED_BYTES` and `WARN_UNZIPPED_BYTES`, alongside `LAYER_PUBLISHABLE`. The build
measures the package, writes `build-stats.json` next to the artifact and appends a row to the
job summary; an oversized package that claims to be publishable fails the build instead of
failing a release at `PublishLayerVersion`.

## Container images

Every runtime also has a `container` block, merged from `REPO_DEFAULTS` and the family defaults:

```json
"container": {
  "enabled": true,
  "profile": "distroless",
  "base_image": "gcr.io/distroless/cc-debian13:nonroot",
  "libs_base_image": "debian:13-slim",
  "registry": "ghcr.io/tartaruscode/lambda-runtime-factory",
  "workdir": "/var/task",
  "layer_root": "/opt",
  "entrypoint": ["/opt/bootstrap"],
  "packages": [],
  "links": {},
  "smoke_test": [],
  "env": {"HOME": "/tmp", "TMPDIR": "/tmp", "LAMBDA_TASK_ROOT": "/var/task"},
  "user": "65532:65532"
}
```

`profile` decides what the image *is*, and the two answers are not interchangeable:

- **`distroless`** — a runtime base. No shell, no package manager; the entrypoint must be a
  binary inside the payload, and the libraries the payload links against are staged from
  Debian 12. This is the default and what every interpreter runtime uses.
- **`build`** — a build environment, for a toolchain. A toolchain image is only useful as a
  Dockerfile build stage, and a `RUN` step needs a shell, so the base is a shell-bearing image
  (`debian:12-slim`, `alpine:3.22`). The manifest then declares `packages` (installed with the
  base's own package manager), `links` (symlinks the payload needs to be runnable, e.g. musl's
  `libc.so` and rustc's sysroot) and `smoke_test` (the toolchain command that proves the image
  works — a build image has no handler to invoke). `validate-runtimes` refuses a `build`
  profile without a `smoke_test`, and refuses `packages` on a `distroless` one.

The image is built from the **same** `${LAYER_ROOT}` as the zip layer, so the two artifacts
cannot drift:

```bash
make build RUNTIME=pypy311 ARCH=x86_64        # payload
make container RUNTIME=pypy311 ARCH=x86_64    # image from that payload
make container-invoke RUNTIME=pypy311 ARCH=x86_64   # RIE smoke test (or the toolchain's own)
bash tools/bin/audit-container pypy311 x86_64 # Grype scan of the image
```

### Variants: the production image and the debug image

Every distroless runtime publishes two images from one payload:

- **`<version>`** — the production image: no shell, `USER 65532:65532`, and the runtime's
  entrypoint. This is the one a function uses.
- **`<version>-debug`** — the *same payload and entrypoint* on
  `gcr.io/distroless/cc-debian13:debug-nonroot`, which also carries a busybox shell
  (`/busybox/sh`, already on `PATH`). Same uid, same invocation: what you inspect is what
  failed. It is not meant to be deployed.

The variant travels as `container.debug_base_image` in the manifest (empty means "this family
has no debug variant", which is how the `build` profiles are configured: they already have a
shell). Everything else — entrypoint, user, environment, workdir — is shared, and
`check-container-variants` tests exactly that claim: the standard image must fail to execute
`/bin/sh` and `/busybox/sh`, the debug image must succeed, and the two must agree on
`Entrypoint` and `User`.

```bash
make container        RUNTIME=pypy311 ARCH=x86_64   # standard
make container-debug  RUNTIME=pypy311 ARCH=x86_64   # the debug variant
make container-variants RUNTIME=pypy311 ARCH=x86_64 # assert both claims
bash tools/bin/container-variants pypy311 x86_64    # standard debug | standard
```

CI builds the debug variant for every cell that has one and runs the variant check, so a base
swap that quietly dropped the entrypoint or the user fails there rather than in production.

### The entrypoint must not need a shell

`gcr.io/distroless/cc-debian13` has no `/bin/sh`, no `/bin/bash` and no `/usr/bin/env`. That
makes the entrypoint part of the contract:

- **pypy / graalpy** — `["/opt/pypy/bin/pypy", "/opt/bootstrap"]` (the interpreter, then the
  script). The shebang is still there for the zip layer, but the image does not rely on it.
- **bun / deno** — the runtime binary itself, with the helper and its flags in argv:
  `["/opt/deno/deno", "run", "--allow-net", …, "/opt/deno/lib/lambda_runtime_deno/runtime.ts"]`.
  The environment the shell bootstrap used to export (`HOME=/tmp`, `DENO_DIR=/tmp/.deno`) is
  initialized at the top of `helpers/lambda_runtime_*/runtime.ts`, so the zip layer and the
  image share one code path.
- **go / rust** — `build-runtime` compiles `tools/launcher/bootstrap.c` into `/opt/bootstrap`
  with `cc -O2 -static`, replacing the `#!/bin/sh` exec wrapper. The same ELF runs on
  `provided.al2023` and on distroless, which is what the *layer* needs. Their images are
  `build` profiles and do not use it: a toolchain image is a build stage, so its entrypoint is
  `/bin/sh` and the toolchain is on `PATH`. If the build host is a different architecture, the
  script is kept and the build warns.

`container_image.assert_native_entrypoint` enforces this before the Docker build: a shebang
script or a missing path fails with a message naming the file.

### Shared libraries and the filesystem contract

The base ships glibc, libm, libpthread, librt, libgcc, libstdc++, OpenSSL and ca-certificates —
not zlib, bzip2 or ncurses. `container_image.py` scans the payload's `DT_NEEDED` entries,
maps anything the base lacks to a Debian package (`PACKAGE_FOR_SONAME`), and generates a
`debian:<N>-slim` builder stage — the same release the base is built from, `debian:13-slim` for
trixie — that copies those `.so` files into the final image's
`/usr/lib/<triplet>`. An unmapped soname fails the build with its name instead of producing an
image that cannot `dlopen`. The final stage only uses `COPY`, `ENV`, `WORKDIR`, `USER` and
`ENTRYPOINT` — never `RUN`, which would need a shell.

`/tmp` is `1777` in the base and `/var/task` is the `WORKDIR`; `build-container` verifies both
(plus `/opt`) from the exported rootfs before reporting success, because Lambda's contract
requires them. That same export measures the image's **unpacked** size, which is what the job
summary and `build-stats.json` record: `docker image inspect .Size` is the compressed size on a
containerd image store and the uncompressed one on the classic overlay2 store, so the same image
read 82,283,585 B on a CI runner and 251,143,202 B on a developer box. The rootfs sum is the
number Lambda counts against its 10 GB container limit, and it does not depend on the daemon.

### Building arm64 on an x86 host

`--platform linux/arm64` needs qemu registered with the kernel; without it the container dies
with `exec format error`. Docker Desktop registers it, a plain `dockerd` does not:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker run --rm --platform linux/arm64 alpine:3.22 uname -m    # aarch64
```

Emulation is slow, and for a GraalVM-based payload also a little flaky: GraalPy's first
invocation takes ~19 s instead of ~2 s, and the first request after a container start can be
reset once before it answers — retry before suspecting the image. CI is the authoritative
check: `ARCH_RUNNERS` assigns `ubuntu-26.04-arm` to every arm64 cell, so the build, the Grype
scan and the RIE smoke test all run natively there. Use qemu only to catch an
architecture-specific mistake early. One thing it cannot do is `strip`: it refuses a foreign
payload, so a cross-arch build packages unstripped binaries and comes out slightly larger than
the same build on a native runner. The multi-arch merge step can be exercised without GHCR by
pushing both per-arch tags to a throwaway registry (`docker run -d -p 5000:5000 registry:2`)
and running the `imagetools create` line from `release-runtime.yml` against it.

### Publishing and consuming

Every runtime publishes to **both** registries in `container.registries`: GHCR
(`ghcr.io/tartaruscode/lambda-runtime-factory/<runtime>`) and Amazon ECR Public
(`public.ecr.aws/tartaruscode/lambda-runtime-factory/<runtime>`). GHCR is the browsable copy;
ECR Public exists because a Lambda *function* cannot pull from GHCR at execution time, and
gives anonymous, high-bandwidth pulls inside AWS.

The alias in an ECR Public path is the registry namespace, and the path after it is the
repository name: `public.ecr.aws/tartaruscode/lambda-runtime-factory/pypy311` is repository
`lambda-runtime-factory/pypy311`. A registry has a **default alias** — `tartarusgroup` for this
account, assigned with its first public repository — plus any **custom aliases** requested in the
ECR console under Public registry settings, and *any active alias* can be used for pulls. That
is why the manifest names `tartaruscode`: it matches the project and the GHCR path, so only the
registry host differs between the two. `ensure-container-repos` creates the repository
idempotently and fails with the account's real aliases when `tartaruscode` is not among them,
which is a far better error than a push that goes nowhere.

While a custom alias is still pending in the console, the release has to publish under the alias
the account actually has. The `ECR_PUBLIC_ALIAS` repository variable does that: it rewrites the
alias segment of the `public.ecr.aws/...` entries only, so the manifest keeps naming the alias the
docs and the image URIs use, and flipping over when the alias is approved is a settings change
rather than a commit. Empty or unset means "use the manifest".

`push-container --variant debug` uses `<version>-<arch-slug>-debug`, and the release's
`merge-containers` job calls `merge-container-tags`, which combines the two per-arch images of
each variant into `<version>`, the version line, and the moving tag — `latest` for the standard
image, `debug` for the debug one — once per registry. A dispatch that targeted one architecture
leaves the per-arch tags alone with a notice instead of failing.

SOCI publishing produces a **second image per architecture**, not a tag on the published one:
`soci convert --standalone` packages the index into a new image (SOCI Index Manifest v2), and
`merge-containers` merges the two into `<version>-soci` plus the version line's `-soci`. There is
deliberately no moving `-soci` tag: a v2 image cannot have its index added or removed, so a
function pinned to a moving tag would change what it runs the moment the next release moved it.
Lambda lazy-loads an image when the function runs on **Lambda Managed Instances** and its image
URI points at the `-soci` tag.

Nothing in that path needs containerd or root. `soci create` and `soci push` resolve the image
through containerd's image service, which a runner that builds with Docker does not have, so the
release pulls the per-arch image into an OCI layout with `oras`, converts it with `soci convert
--standalone`, and pushes the result back. The push is verified rather than assumed: the tag must
be an index whose two entries are the image manifest and the index manifest, cross-annotated, and
only for the architecture it was built from — an unindexed image would still start, just slowly.

Both tools are pinned (`SOCI_VERSION`, `ORAS_VERSION`). The work is opt-in (`publish_soci: true`)
because it only pays off on Managed Instances and it adds an image, an index and the ztoc blobs
per architecture (the original layers are referenced, not re-uploaded).

```dockerfile
# A runtime image is a Lambda container image: entrypoint = runtime, CMD = handler.
FROM ghcr.io/tartaruscode/lambda-runtime-factory/graalpy312:3.12
COPY app.py ${LAMBDA_TASK_ROOT}/
CMD ["app.handler"]
```

The Go and Rust images are build environments (`FROM …/go126:1.26 AS build`); deliver the
compiled bootstrap on `public.ecr.aws/lambda/provided:al2023`, as shown in the README.


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

That only works because the package is deterministic. `build-runtime` pins every entry's mtime
and zips with `-X`, because a zip that embeds "now" produces a different hash on every build —
and then the reuse above can never match.

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
