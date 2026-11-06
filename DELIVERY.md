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
| OCI image | 10 GB uncompressed | **Every** runtime, including the toolchains |

The image is the permanent answer for the toolchains: `go126`, `go127`, `rust194`, `rust198` and
`rust199` publish as multi-arch images (`ghcr.io/tartaruscode/lambda-runtime-factory/<id>`) and
are first-class release artifacts.

An image is one of two shapes, and the manifest's `container.profile` picks which:

- **`distroless`** — a runtime base, for the interpreter runtimes. The base is
  `gcr.io/distroless/cc-debian12`, which removes the shell the old bootstrap depended on and
  keeps the OS CVE surface to glibc, libstdc++, OpenSSL and ca-certificates.
- **`build`** — a build environment, for the Go and Rust toolchains. A toolchain image is only
  useful as a Dockerfile build stage (`FROM …/go126:1.26 AS build` then `RUN go build`), and a
  `RUN` step needs a shell, which distroless does not have. The bases are `debian:12-slim` for
  Go and `alpine:3.22` for Rust — the musl toolchain's own binaries are dynamically linked
  against musl (`NEEDED libc.so`, interpreter `/lib/ld-musl-x86_64.so.1`), so a glibc base
  cannot run `rustc` at all. The image installs `libgcc`, `gcc` and `musl-dev`, links musl's
  `libc.so` and rustc's sysroot into place, and its smoke test runs the toolchain
  (`go version`, `rustc --version`) rather than invoking a handler it does not have.

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
  `gcr.io/distroless/cc-debian12:debug-nonroot`, which carries a busybox shell. It exists so a
  failing image can be inspected instead of guessed at, and `check-container-variants` asserts
  the claims: the production image has no shell at all, the debug image runs `/busybox/sh` as
  the same uid with the same entrypoint.
- The images go to GHCR **and** Amazon ECR Public. A Lambda function cannot pull from GHCR at
  execution time, so ECR Public is the copy that matters inside AWS.
- The SOCI index is generated after the multi-arch tag exists, for ECR only: Lambda uses it to
  start an image lazily, which takes image size out of the cold-start path.

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
| `graalpy312` | x86_64 | 256,802,903 | 98.0% | yes |
| `graalpy312` | arm64 | 259,170,629 | 98.9% | yes |

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

### GraalPy 3.12: published, with a narrow margin

GraalPy is dominated by one file: `graalpy/lib/libpythonvm.so`, a GraalVM native image that is
~225 MB after stripping. Everything else in the distribution — the Python stdlib, the launcher
binaries, the native modules — adds ~32 MB. That puts a floor around 225 MB, so the 180–205 MB
range is not reachable for this distribution by pruning: there is nothing else of that size to
remove. The real numbers are 256.8 MB (x86_64) and 259.2 MB (arm64), both under the ceiling but
leaving only **3–5 MB** for the function's own code. The layer publishes; a function that
attaches it must stay small, and the build emits the over-200 MB warning so the margin is
visible in every release summary.

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
