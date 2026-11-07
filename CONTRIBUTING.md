# Developing lambda-runtime-factory

Building, testing, releasing and extending the runtime packages. For what the repository
publishes — tag names, registries, architectures and how to verify them — see the
[README](README.md).

## Build environment

- Linux or WSL is required for build and release commands
- The shared tooling assumes native Linux tools such as `bash`, `tar`, `zip`, `unzip`, `curl`,
  `sha256sum`, `cc`, and `make`. `cc` links the Go/Rust bootstrap launcher; without it the shell
  bootstrap is kept
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
make build RUNTIME=pypy311            # stage the payload
make check RUNTIME=pypy311            # validate the manifest and compile its Python
make test                             # repo test suite (pytest)
```

Image and supply-chain work:

```bash
make container        RUNTIME=pypy311 ARCH=x86_64   # the Iron Bank UBI9 image
make container-check  RUNTIME=pypy311 ARCH=x86_64   # assert its STIG contract
make container-invoke RUNTIME=pypy311 ARCH=x86_64   # smoke test, rootless + read-only
make compliance       RUNTIME=pypy311 ARCH=x86_64   # SBOMs, Grype report, provenance
make container-push   RUNTIME=pypy311 ARCH=x86_64   # push the per-arch image
make attest           RUNTIME=pypy311 ARCH=x86_64   # cosign sign + attach the bundle
```

Nothing here needs AWS credentials. The smoke test, the scan and the push use the registry you are
logged in to; a build with no `registry1.dso.mil` credentials uses the Red Hat fallback automatically
(`USE_FALLBACK_UBI=1 make container …`). `make attest` needs an OIDC token for keyless signing, so it
only signs in CI — locally it says so and exits 0.

## The runtime manifest

Each runtime package declares its build and release contract in `runtimes/<id>/runtime.json`:
the runtime family, its version or distribution identifier, and any override of the family
defaults. Most details are derived from `RUNTIME_FAMILY_DEFAULTS` in
`tools/runtime_lib/runtime_manifest.py`, so a runtime that follows an established family layout
keeps a very small manifest. Anything derived (tag names, registries, paths, bootstrap and helper
locations) can be inspected with:

```bash
make list-runtimes
python3 tools/runtime_lib/runtime_manifest.py validate
python3 tools/runtime_lib/runtime_manifest.py env --runtime pypy311 --arch x86_64
```

The manifest list is the single source of truth for the CI and release matrices — both call
`runtime_manifest.py matrix` through the same composite action, and both run every runtime. There
is no per-runtime opt-out: one artifact per runtime means nothing to narrow.

There is no byte budget any more. The 250 MB unzipped ceiling applied to Lambda layers, and the
layer is gone; the image has a 10 GB limit that no runtime approaches. `build-stats.json` now records
the staged payload size, and the number that matters — the unpacked image size — is measured from the
exported rootfs by `build-container` and recorded in `container.json`.

## Container images

Every runtime also has a `container` block, merged from `REPO_DEFAULTS` and the family defaults:

```json
"container": {
  "enabled": true,
  "base_image": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest",
  "fallback_base_image": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
  "base_image_digest": "",
  "nonroot_uid": 1001,
  "nonroot_gid": 1001,
  "packages": ["ca-certificates"],
  "unsatisfied_sonames": [],
  "registry": "ghcr.io/tartaruscode/lambda-runtime-factory",
  "registries": ["ghcr.io/tartaruscode/lambda-runtime-factory", "public.ecr.aws/tartaruscode/lambda-runtime-factory"],
  "workdir": "/var/task",
  "layer_root": "/opt",
  "entrypoint": ["/opt/bootstrap"],
  "smoke_test": {},
  "env": {"HOME": "/tmp", "TMPDIR": "/tmp", "LAMBDA_TASK_ROOT": "/var/task", "GOCACHE": "/tmp/.cache/go-build", "GOPATH": "/tmp/go", "CARGO_HOME": "/tmp/.cargo", "RUSTUP_HOME": "/tmp/.rustup", "DENO_DIR": "/tmp/.deno", "PYTHONPYCACHEPREFIX": "/tmp/.pycache"},
  "user": "1001:1001"
}
```

There is **one base**. A family manifest may not redeclare `base_image`, `profile`, `user` or
`entrypoint` — `test_no_second_container_base_survives_anywhere` refuses it, because a second base
doubles the container matrix and the tag set for no gain. The only per-family container keys are
`env` (a `PATH` for the toolchains, a `PYTHONPATH` for the interpreters), `smoke_test` and
`unsatisfied_sonames`.

The image is built from the **same** `${LAYER_ROOT}` that `build-runtime` staged, so the two steps
cannot drift, and `build-container` never re-derives the payload — it reads what the build produced,
including the `/opt/runtime-manifest.json` that records where those bytes came from.

```bash
make build RUNTIME=pypy311 ARCH=x86_64        # payload
make container RUNTIME=pypy311 ARCH=x86_64    # image from that payload
make container-check RUNTIME=pypy311 ARCH=x86_64    # assert the STIG contract
make container-invoke RUNTIME=pypy311 ARCH=x86_64   # smoke test (or the toolchain's own)
bash tools/bin/audit-container pypy311 x86_64 # Grype scan of the image
```

### The stage layout

`tools/docker/Dockerfile` is the one canonical file, rendered per runtime by
`container_image.render_dockerfile`: the entrypoint is fixed at `/opt/bootstrap` (the STIG
contract) and only the payload path, the environment and the labels come from the manifest. Keeping
the invariants in a reviewable file rather than in Python string concatenation is deliberate —
`test_ironbank_template_carries_the_stig_invariants` reads the file, not the generator.

Three rules matter when you touch this path:

- **Libraries are installed, never copied.** `container_image.required_ubi_packages` maps the
  payload's `DT_NEEDED` sonames to UBI9 packages and the image installs them with
  `microdnf install -y --nodocs --setopt=install_weak_deps=0`. Cherry-picking resolved `.so` files
  would leave `/var/lib/rpm` without the owning package — so Grype cannot match a RHEL backport and
  reports a patched library as vulnerable — and `/etc/ld.so.cache` stale. `rpm` and `/var/lib/rpm`
  therefore stay in the image, and only `microdnf`/`dnf`/`yum` are removed;
  `container_image.verify_image_layout` fails a build that lost the RPM database.
  - A soname the base genuinely cannot provide is empty repo-wide. `rust-musl` declares one
    (`libc.so`, musl's soname, which UBI9 has no package for) in
    `container.unsatisfied_sonames`, because the binaries needing it are the ones the image's
    runtime contract does not run. Add a name there only with that kind of reason.
- **`registry1.dso.mil` needs credentials.** Set `IRONBANK_USERNAME`/`IRONBANK_TOKEN` as repository
  secrets and the workflows log in; without them (`USE_FALLBACK_UBI=1`, or `IRONBANK_TOKEN` unset in
  CI) the build uses `registry.access.redhat.com/ubi9/ubi-minimal`, which serves the same userland.
  `BASE_IMAGE_DIGEST` pins the base so Iron Bank's daily rebuild cannot move it under a reproducible
  build.
- **Root CAs come from `certs/*.crt`.** The builder stage copies them into
  `/etc/pki/ca-trust/source/anchors/` and runs `update-ca-trust extract`; the final stage points
  every TLS stack at the resulting bundle. An empty directory is valid.

### The STIG contract, and how it is proven

`USER 1001:1001`, `WORKDIR /var/task`, `ENTRYPOINT ["/opt/bootstrap"]`, no package manager, and
every writable path (`HOME`, `TMPDIR`, `GOCACHE`, `GOPATH`, `CARGO_HOME`, `RUSTUP_HOME`, `DENO_DIR`,
`PYTHONPYCACHEPREFIX`) under `/tmp`, because `/` is read-only at run time.

Three checks read that back from real bytes rather than trusting the Dockerfile:

- `build-container` runs `container_image.verify_image_layout` on the exported rootfs: `/tmp` is
  `1777`, `/var/task` and `/opt` exist, no `microdnf`/`dnf`/`yum`, and `/var/lib/rpm` is present.
- `make container-check` (`tools/bin/check-container`) reads `USER`, `Entrypoint` and `WorkingDir`
  off the image and then runs a probe as that UID on a read-only rootfs with a uid-owned `/tmp`
  tmpfs, asserting `/opt/bootstrap` is readable, `/tmp` is writable, `/var/task` exists and the
  package managers are gone.
- `local-invoke-container` runs the smoke test under exactly those flags —
  `--read-only --tmpfs /tmp:rw,exec,size=512m,uid=1001,gid=1001 --user 1001:1001` — with
  `aws-lambda-rie` bind-mounted read-only from the host cache. The emulator is never baked into a
  published image.

That same rootfs export measures the image's **unpacked** size, which is what the job summary and
`build-stats.json` record: `docker image inspect .Size` is the compressed size on a containerd image
store and the uncompressed one on the classic overlay2 store, so the same image read 82,283,585 B on
a CI runner and 251,143,202 B on a developer box. The rootfs sum is the number Lambda counts against
its 10 GB container limit, and it does not depend on the daemon.

### The entrypoint, and the families that have no handler

One entrypoint for every family: `/opt/bootstrap`, which applies the Lambda filesystem contract and
execs the runtime.

Per family:

- **pypy / graalpy** — `bootstrap.py3` starts `#!/opt/pypy/bin/pypy` (or graalpy), so a direct
  `execve` of `/opt/bootstrap` resolves the interpreter. The image carries the layer's
  `site-packages` on `PYTHONPATH`.
- **bun / deno** — the bootstrap is a `#!/bin/sh` wrapper that `exec`s the runtime with the helper
  and its flags. The environment it exports (`HOME=/tmp`, `DENO_DIR=/tmp/.deno`) is also initialized
  at the top of `helpers/lambda_runtime_*/runtime.ts`, so the bootstrap and the helper share one code
  path.
- **go / rust** — `build-runtime` compiles `tools/launcher/bootstrap.c` into `/opt/bootstrap` with
  `cc -O2 -static`, replacing the `#!/bin/sh` exec wrapper. The same ELF runs on `provided.al2023`
  and inside the image. If the build host is a different architecture, the script is kept and the
  build warns.

An image whose runtime has no handler to invoke declares its own smoke test instead
(`container.smoke_test`), which runs under the same rootless read-only flags:

```json
// go-toolchain: the distribution is glibc-linked, so it runs on this base
"smoke_test": {"argv": ["go", "version"], "expect_version": true}

// rust-musl: rustc is musl-linked and this base ships no musl loader, so the
// launcher's own handler contract is what gets asserted
"smoke_test": {"argv": ["/opt/bootstrap"], "expect_exit": 127, "expect_output": "_HANDLER is not set"}
```

### Building arm64 on an x86 host

`--platform linux/arm64` needs qemu registered with the kernel; without it the container dies
with `exec format error`. Docker Desktop registers it, a plain `dockerd` does not:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker run --rm --platform linux/arm64 registry.access.redhat.com/ubi9/ubi-minimal uname -m  # aarch64
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

`push-container` tags the per-arch image `<version>-<arch-slug>` (and `-soci` for the SOCI copy),
and the release's `merge-containers` job calls `merge-container-tags`, which combines the per-arch
images into `<version>`, the version line, and the moving tag — `latest`, on a tagged release. A
dispatch that targeted one architecture leaves the per-arch tags alone with a notice instead of
failing.

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
FROM ghcr.io/tartaruscode/lambda-runtime-factory/graalpy313:3.13
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

`.github/workflows/ci.yml` runs on every push to `main`, every PR (docs-only changes are
skipped) and on demand via `workflow_dispatch`. It has **two tiers**, so a work-in-progress commit
does not occupy all 24 runners:

- **Fast tier** — a draft PR, or a PR whose labels do not include `ready-to-merge`, runs only the
  cells whose files changed under `runtimes/` (computed by the `runtime-matrix` action from
  `git diff --name-only origin/<base>...HEAD`), or a single `pypy311` canary cell when nothing
  under `runtimes/` changed at all.
- **Full tier** — a push to `main`, a `workflow_dispatch`, or a PR that left draft and carries
  `ready-to-merge` runs the full matrix.

Between the two:

- `repo-checks` (once, < 1 min): manifest validation, `bash -n` over `tools/bin/`, a compile check
  per runtime and the online archive-URL check, plus the test suite
- `grype-db` (once): refreshes and caches the ~2.8GB vulnerability database for the matrix
- `runtime-checks` (matrix): builds every runtime × architecture, scans the ones whose pinned
  payload changed, builds the Iron Bank image, scans and smoke-tests it, and checks its STIG
  contract (`check-container`); on x86 it additionally runs the local SAM smoke test
- caches: upstream runtime downloads, the built package, the Grype binary and database
- artifacts: built zips upload on **push to `main` only**, `retention-days: 7`
- superseded PR runs cancel via workflow concurrency

Docker Buildx is set up with the `docker-container` driver, and every image build passes
`--cache-from type=registry,ref=$CONTAINER_CACHE_REF`
(`ghcr.io/tartaruscode/lambda-runtime-factory/<runtime>:buildcache-<arch>`, derived in the
manifest so no caller invents its own). Only the default branch adds `--cache-to …,mode=max`, so a
PR's cache never lands on a ref nothing else can read. That is what makes the base pull, the
certificate extraction and the library installation cheap on a re-run.

Three composite actions in `.github/actions/` hold the whole per-cell procedure so the workflows
cannot drift apart:

- `runtime-matrix` — the build/publish matrix, generated from the manifest list (and narrowed to
  the changed runtimes for the fast tier)
- `runtime-build-env` — the download, package, Grype-binary and Grype-database caches
- `build-and-audit-runtime` — archive tooling, Buildx setup, `make build`, the image build, the
  smoke test, the STIG contract check and `tools/bin/generate-compliance-artifacts`

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
