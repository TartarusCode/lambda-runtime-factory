# Delivering runtimes that do not fit a layer

In run `42121477502`, 12 of 24 publish cells failed `PublishLayerVersion`:

```
InvalidParameterValueException ... PublishLayerVersion:
Unzipped size must be smaller than 262144000 bytes
```

The affected runtimes were `go126`, `go127`, `graalpy312`, `rust194`, `rust198` and `rust199`,
on both architectures. Every one of them uploads to S3 without complaint; only the layer publish
refuses, because the limit counts **unzipped** bytes.

That is Lambda's deployment-package ceiling, quoted from the quotas page:

> **250 MB** — the maximum size of the contents of a deployment package, including layers and
> custom runtimes. (unzipped)

Three properties matter:

- It counts unzipped bytes. The zipped package is usually a third of the size.
- It covers the function **and every layer attached to it**, so splitting a runtime across
  several layers does not help (a function may carry at most 5 layers).
- It is not adjustable.

## Two artifacts instead of one

The factory now produces both, from the same `${LAYER_ROOT}` payload:

| Artifact | Ceiling | Covers |
| --- | --- | --- |
| Lambda zip layer | 250 MB unzipped, **shared with the function's code** | Runtimes that fit with usable headroom |
| OCI image (distroless) | 10 GB uncompressed | **Every** runtime, including the toolchains |
| OCI image (Iron Bank) | 10 GB uncompressed | **Every** runtime, for a DoD ATO that requires UBI9 |

The image is the permanent answer for the toolchains: `go126`, `go127`, `rust194`, `rust198` and
`rust199` publish as multi-arch images (`ghcr.io/tartaruscode/lambda-runtime-factory/<id>`) and
are first-class release artifacts.

An image is one of two shapes, and the manifest's `container.profile` picks which:

- **`distroless`** — a runtime base, for the interpreter runtimes. The base is
  `gcr.io/distroless/cc-debian13`, which removes the shell the old bootstrap depended on and
  keeps the OS CVE surface to glibc, libstdc++, OpenSSL and ca-certificates.
- **`build`** — a build environment, for the Go and Rust toolchains. A toolchain image is only
  useful as a Dockerfile build stage (`FROM …/go126:1.26 AS build` then `RUN go build`), and a
  `RUN` step needs a shell, which distroless does not have. The bases are `debian:12-slim` for
  Go and `alpine:3.22` for Rust — the musl toolchain's own binaries are dynamically linked
  against musl (`NEEDED libc.so`, interpreter `/lib/ld-musl-x86_64.so.1`), so a glibc base
  cannot run `rustc` at all. The image installs `libgcc`, `gcc` and `musl-dev`, links musl's
  `libc.so` and rustc's sysroot into place, and its smoke test runs the toolchain
  (`go version`, `rustc --version`) rather than invoking a handler it does not have.

## The Iron Bank artifact

Every runtime builds a **second** image from the same payload on
`registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal`. It is not a replacement for the distroless
image — distroless is the smallest CVE surface and Iron Bank is the base a DoD ATO accepts — so a
consumer picks, and both are published under their own tags:

```
ghcr.io/tartaruscode/lambda-runtime-factory/<id>:<version>            # distroless
ghcr.io/tartaruscode/lambda-runtime-factory/<id>:<version>-debug      # distroless + busybox
ghcr.io/tartaruscode/lambda-runtime-factory/<id>:<version>-ironbank   # UBI9, hardened
```

The stage layout lives in **one** canonical template, `tools/docker/Dockerfile.ironbank`, which
`tools/runtime_lib/container_image.py` renders per runtime. Keeping it in a file (rather than in
Python string concatenation) is what makes the STIG invariants reviewable:

- **Stage 1 (`builder`)** copies `certs/*.crt` into `/etc/pki/ca-trust/source/anchors/` and runs
  `update-ca-trust extract`, so an injected DoD root CA is trusted by OpenSSL, Python, Node and
  Deno through the single bundle the final stage points every TLS stack at
  (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `DENO_CERT`, `NODE_EXTRA_CA_CERTS`).
- **Stage 2 (`runtime`)** copies the extracted trust store, copies the payload to `/opt` (owned by
  the runtime UID), normalizes permissions (`chmod -R a+rX /opt`), installs any missing runtime
  library **with `microdnf`**, then removes the package manager again. It runs as `USER 1001:1001`
  with `WORKDIR /var/task` and `ENTRYPOINT ["/opt/bootstrap"]`.

Two details are deliberate:

- **Libraries are installed, never cherry-picked.** The build derives a package list from the
  payload's `DT_NEEDED` scan (`container_image.required_ubi_packages`) and installs it with
  `microdnf install -y --nodocs --setopt=install_weak_deps=0`. Copying resolved `.so` files by
  hand would leave `/var/lib/rpm` without the owning package — Grype then cannot match a RHEL
  backport and reports false positives against a payload that is actually patched — and
  `/etc/ld.so.cache` stale. `rpm` itself stays in the image for exactly that reason.
- **The base falls back when credentials are absent.** `registry1.dso.mil` needs a login, so a
  build without `IRONBANK_TOKEN` (an external PR, a fork, a local run) uses
  `registry.access.redhat.com/ubi9/ubi-minimal`, which serves the same UBI9-minimal userland. The
  release logs in and builds from the primary; `BASE_IMAGE_DIGEST` pins the base when Iron Bank's
  daily rebuild would otherwise move it under a reproducible build.

`tools/bin/local-invoke-container` proves the contract rather than asserting it: the smoke test
runs the image with `--read-only`, a `--tmpfs /tmp:rw,exec,size=512m,uid=1001,gid=1001` — the only
writable path — and `--user 1001:1001`, with `aws-lambda-rie` bind-mounted read-only from the host
cache. The emulator is never baked into the published image.

Measured locally (x86_64, unpacked bytes from the exported rootfs, `USE_FALLBACK_UBI=1`):
`graalpy312` — distroless 284,530,181 B, Iron Bank 359,244,382 B; both answer an RIE invocation
with HTTP 200 under UID 1001 on a read-only rootfs. The Iron Bank image carries no package manager
(`microdnf`/`dnf` removed, `rpm` and `/var/lib/rpm` kept) and its `/opt/bootstrap` is owned by
1001:1001.


Measured on the built images (x86_64, unpacked bytes summed from the exported rootfs): `go126`
251,136,536 B, `rust198` 1,521,518,187 B, `bun13` 116,422,312 B, `pypy311` 113,869,671 B.
`go build` and `cargo build` were both run inside the published images, and the resulting
binaries executed. The arm64 images were built and exercised too — `go126` 274,243,748 B answers
`go version go1.26.10 linux/arm64`, and `graalpy312` 293,547,778 B returned a 200 from an RIE
invocation with an ARM `libz.so.1` staged into it — and the two per-arch tags were combined into
a real multi-arch manifest list against a throwaway registry to exercise the release's merge
step. See CONTRIBUTING.md for the qemu setup and why CI's native arm runners are the
authoritative check.

The size is measured from the exported rootfs rather than from `docker image inspect .Size`,
because that field is the *compressed* size on a containerd image store and the uncompressed one
on the classic overlay2 store: the same image read 82,283,585 B on a CI runner and 251,143,202 B
on a developer box. The rootfs sum is the unpacked size Lambda counts against its 10 GB limit,
and it is the same number wherever it is measured.

## What else the release publishes

Beyond the two artifact shapes, every runtime publishes a **debug variant** and, on ECR, a
**SOCI index**:

- `<version>-debug` is the same payload and invocation on
  `gcr.io/distroless/cc-debian13:debug-nonroot`, which carries a busybox shell. It exists so a
  failing image can be inspected instead of guessed at, and `check-container-variants` asserts
  the claims: the production image has no shell at all, the debug image runs `/busybox/sh` as
  the same uid with the same entrypoint.
- The images go to GHCR **and** Amazon ECR Public. A Lambda function cannot pull from GHCR at
  execution time, so ECR Public is the copy that matters inside AWS.
- The SOCI copy is a **second image** per architecture (`<version>-soci`, `<line>-soci`), made by
  `soci convert --standalone` and merged like any other multi-arch image: Lambda lazy-loads the
  image whose URI the function is given, and only on Lambda Managed Instances. It is opt-in
  (`publish_soci: true`) because it only pays off there, and it needs no containerd — the
  conversion runs on an OCI layout that `oras` pulls and pushes.

## What the bytecode experiment showed (and why it is off)

Precompiling the interpreter's standard library to `.pyc` was expected to cut cold-start time.
Measured on `pypy311` x86_64, 12 cold starts per configuration, same handler:

| Payload | Unzipped | Init median | Init mean |
| --- | --- | --- | --- |
| sources only (default) | 90.3 MB | 1693 ms | 1675 ms |
| `PRECOMPILE_BYTECODE=1` | 106.5 MB | 1713 ms | 1744 ms |

It costs 16 MB and buys nothing measurable: the interpreter's own startup dominates this
handler's init, and PyPy executes source quickly. It stays as an opt-in for a family where
imports are the bottleneck, and a runtime close to the layer ceiling drops the generated
`.pyc` files again rather than failing a build over an optimisation.

What was pruned instead, and is worth keeping: stdlib test suites, GUI toolkits, headers,
static archives and debug symbols — and nothing else, because the payloads ship no locale data
(glibc provides it) and their bundled `zoneinfo` is 52 KB that timezone-aware code reads.

## What slimming actually bought

Measured on the built artifacts (`build-stats.json`, sum of zip entry sizes):

| Runtime | Arch | Unzipped bytes | % of 262,144,000 | Layer published |
| --- | --- | --- | --- | --- |
| `pypy311` | x86_64 | 90,278,281 | 34.4% | yes |
| `go126` | x86_64 | 176,269,276 | 67.2% | no — toolchain image |
| `graalpy313` | x86_64 | 250,195,312 | 95.4% | yes |
| `graalpy312` | x86_64 | 256,817,392 | 98.0% | yes |
| `graalpy312` | arm64 | 259,170,629 | 98.9% | yes |

Every build writes `${ARTIFACT_DIR}/build-stats.json` with the exact byte budget
(`zipped_bytes`, `unzipped_bytes`, `max_unzipped_bytes`, `warn_unzipped_bytes`, `headroom_bytes`,
`percent_of_quota`, `layer_publishable`, `level`) and the job summary prints it as a row whose
`Targets` column names the artifacts the cell delivers (`Layer + Iron Bank Container` or
`Iron Bank Container Only`). Before zipping, the payload's permissions are normalized
(`chmod -R a+rX "${LAYER_ROOT}"`), so a file upstream happened to ship mode 0600 cannot be a
permission-denied under the arbitrary non-root UID Lambda runs the function with.

Slimming prunes stdlib test suites, GUI toolkits (`tkinter`, `turtledemo`, and the compiled
`_tkinter` module that pulled in X11/fontconfig/freetype), `ensurepip`, `idlelib`, `lib2to3`,
`pydoc_data`, C headers, static archives and external `.debug` scripts, then strips ELF debug
symbols. It never removes `*.dist-info`, `*.egg-info`, license or version files: that metadata is
how vulnerability scanning identifies a component, and the audit runs after the slimming so it
scans exactly what ships.

Pruning is by path, not by bare directory name. `doc`, `test`, `api` and `obj` are also real
package directories deeper in a Go tree — `src/go/doc` *is* the stdlib `go/doc` package — and the
Go toolchain compiles the standard library from source, so a name-based prune at any depth
deletes code that `go build` needs. Those directories are pruned at the payload root only;
`testdata`, which is never an importable package, is the one name pruned at any depth.

### GraalPy 3.12 and 3.13: published, with a narrow margin

GraalPy is dominated by one file: `graalpy/lib/libpythonvm.so`, a GraalVM native image that is
~225 MB **after stripping** (`file` reports it as already stripped, and `strip --strip-unneeded`
finds nothing to remove). Everything else in the distribution — the Python stdlib, the launcher
binaries, the native modules — adds ~30 MB. That puts a hard floor around 225 MB, so a sub-210 MB
layer is not reachable for this distribution by pruning: there is nothing else of that size to
remove, and the only remaining lever would be deleting the VM itself. The real numbers are
256.8 MB (`graalpy312` x86_64), 250.2 MB (`graalpy313` x86_64) and 259.2 MB
(`graalpy312` arm64), all under the ceiling but leaving **3–11 MB** for the function's own code.
Both layers publish; a function that attaches one must stay small, and the build emits the
over-200 MB warning so the margin is visible in every release summary.

### Go and Rust: layers are the wrong shape, not just too big

After slimming, Go 1.26 measures 176.3 MB unzipped and would fit a layer. It stays
`layer_publishable: false` for a different reason: a Go "runtime layer" is a compiler
distribution whose bootstrap expects a compiled handler at `/opt/go/bin/<handler>`. A consumer's
code arrives read-only at `/var/task`, so the layer cannot host the handler, and publishing it
would advertise a contract nothing satisfies. The same argument applies to Rust, which is far
over the budget in any case. Both families are delivered as build images.

## Lifting or setting the flag

The flag is the only thing that keeps a runtime out of the layer publish matrix. Because it is
resolved from the **merged** manifest, a family default covers every line — including lines a
version bump has not created yet — and a manifest-level `layer_publishable` survives bumps.
Delete the key to inherit the repo default (`true`), or set it on the family.

A runtime that claims to be publishable but unzips past the ceiling now fails `build-runtime`
in CI, with the overage in the message, instead of failing a release at `PublishLayerVersion`.
An empty publish matrix is refused in preflight rather than reported as a green release.

Source for the limits: <https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html>
