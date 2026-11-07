# lambda-runtime-factory

AWS Lambda **runtime images** — one hardened Iron Bank UBI9 OCI image per runtime, built and
published from one manifest-driven pipeline, with its SBOMs, CVE report and provenance attached to
the digest. Each runtime is an independent package under `runtimes/`. There is no Lambda layer:
see [DELIVERY.md](DELIVERY.md) for why the layer half was removed rather than slimmed.

## What gets published

Every runtime ships **one container image**, plus the compliance bundle that makes it defensible:

|  | Container image | Compliance bundle |
| --- | --- | --- |
| **Runtimes** | every runtime | every runtime |
| **Location** | `ghcr.io/tartaruscode/lambda-runtime-factory/<runtime>` and `public.ecr.aws/tartaruscode/lambda-runtime-factory/<runtime>` | attached to the image digest, and uploaded as a workflow artifact |
| **Architectures** | one multi-arch manifest | per architecture |
| **Address by** | `<version>` (`1.26.10`), `<line>` (`1.26`), `latest` | `oras discover` |

- **The base is Iron Bank UBI9** — `registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal`, the
  DoD-hardened minimal userland, for every runtime. Each image runs as non-root `USER 1001:1001` on a
  read-only rootfs with `/tmp` as the only writable path, carries no package manager, keeps its RPM
  database (which is what gives the SBOM its RHEL lineage), and bakes any `certs/*.crt` root CA into
  its trust store. A build without Iron Bank credentials falls back to
  `registry.access.redhat.com/ubi9/ubi-minimal`, which serves the same userland — the published base
  is recorded in the provenance manifest, so the two are never confused.
- **`-soci` tags** — the same image with a SOCI index bundled in, for functions on **Lambda Managed
  Instances**, which then lazy-load it instead of downloading the whole thing. There is no moving
  `-soci` tag: that image is immutable by design.
- **Every image is signed** keylessly with `cosign` through GitHub Actions OIDC, and carries an SPDX
  SBOM, a CycloneDX SBOM, a Grype report and a provenance manifest as OCI artifacts.

### Verify what you pulled

```bash
cosign verify \
  --certificate-identity-regexp 'https://github.com/TartarusCode/lambda-runtime-factory/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11

oras discover ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11   # SBOMs, CVE report, provenance
cosign download sbom ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11
```

`provenance.json` is the file to read when the question is "what is in this image": it names the
upstream archive and its verified SHA-256, the base image and the digest it resolved to, the image
digest, the unpacked size, the STIG checks and the CVE counts by severity.

## Container images

Use a runtime image directly as a Lambda container image. The image's entrypoint starts the
runtime; `CMD` carries the handler, which the runtime reads exactly as it reads `_HANDLER`:

```dockerfile
FROM ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11

COPY app.py ${LAMBDA_TASK_ROOT}/
CMD ["app.handler"]
```

```bash
docker build -t my-function .
docker push <registry>/my-function
aws lambda create-function --function-name my-function \
  --package-type Image --code ImageUri=<registry>/my-function \
  --role <execution-role-arn>
```

The image is a **runtime image on the Iron Bank UBI9 base**: it runs as `USER 1001:1001` on a
read-only rootfs, so anything installed with `RUN` has to happen in *your* builder stage — see
[examples/docker](examples/docker) for recipes covering pure-Python dependencies, a Bun bundle and
the Go toolchain.

The entrypoint is `/opt/bootstrap`, which applies the Lambda filesystem contract and execs the
runtime, so the handler contract is simple: `CMD` carries the handler name and the runtime reads it
exactly as it reads `_HANDLER`. Any `certs/*.crt` in this repository is baked into the image's trust store, and
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `DENO_CERT` and `NODE_EXTRA_CA_CERTS` all point at the
resulting bundle.

Build it locally with `make container RUNTIME=pypy311` (add `USE_FALLBACK_UBI=1` when you have no
`registry1.dso.mil` credentials), verify the STIG contract with `make container-check`, and
smoke-test it with `make container-invoke`.

The Go and Rust images are **toolchain images**: they carry `go`/`rustc`, not a handler. Their
contract is the same entrypoint — `/opt/bootstrap` execs a compiled handler from
`/opt/<runtime>/bin/<handler>` — so a consumer copies their built binary there:

```dockerfile
FROM ghcr.io/tartaruscode/lambda-runtime-factory/go126:1.26

COPY --from=build --chown=1001:1001 /out/bootstrap /opt/go/bin/myhandler
CMD ["myhandler"]
```

One caveat worth knowing before you pick a base for Rust: the Rust distribution is built for musl,
so `rustc` itself cannot run inside a glibc image (UBI9 ships no musl loader). The image is for
*running* a static musl handler, not for compiling one — compile in a build stage you control.

## Runtimes

| Runtime id | Runtime | Line | Notes |
| --- | --- | --- | --- |
| `bun13` | Bun 1.3 | 1.3 | Bun bootstraps the Runtime API directly |
| `bun14` | Bun 1.4 | 1.4 | Bun bootstraps the Runtime API directly |
| `deno29` | Deno 2.9 | 2.9 | Bootstrap runs `deno` with sandbox flags for the Runtime API |
| `go126` | Go 1.26 | 1.26 | Go toolchain image |
| `go127` | Go 1.27 | 1.27 | Go toolchain image |
| `graalpy313` | GraalPy 3.13 | 3.13 | GraalPy on Python, with the image's site-packages on `sys.path` |
| `pypy311` | PyPy 3.11 | 3.11 | Portable PyPy build from python.org; ships the Runtime API bootstrap and the helper package |
| `pypy312` | PyPy 3.12 | 3.12 | Portable PyPy build from python.org; ships the Runtime API bootstrap and the helper package |
| `rust194` | Rust 1.96 | 1.96 | Rust musl toolchain image |
| `rust198` | Rust 1.98 | 1.98 | Rust musl toolchain image |
| `rust199` | Rust 1.99 | 1.99 | Rust musl toolchain image |


Ids follow the framework and version line: a runtime tracks one line (`go126` is Go 1.26), and a
new line becomes a new runtime rather than mutating an existing one. Run `make list-runtimes` for
the ids the current matrix builds.

## Repository layout

```text
runtimes/<runtime-id>/   runtime.json, bootstrap/, helpers/, checksums/, examples/
tools/bin/               build, audit, upload, publish, local-test entrypoints
tools/runtime_lib/       family defaults, manifest resolution, bump tooling, tests
tools/docker/            Dockerfile — the canonical Iron Bank stage layout
certs/                   extra root CAs baked into the Iron Bank images (*.crt)
.github/workflows/       CI, release, audit and weekly version-bump pipelines
.github/actions/         shared composite actions: matrix, build environment, build + audit
```

## Development

Building, testing, releasing and extending the packages — the build environment, every `make`
target, the local SAM flow, the CI and release pipelines, and how to add a runtime or a version
line — is documented in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache 2.0
