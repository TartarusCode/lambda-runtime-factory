# lambda-runtime-factory

AWS Lambda **runtime packages** for the `provided.al2023` runtime — a Lambda layer and a hardened
Iron Bank UBI9 OCI image per runtime, built and published from one manifest-driven pipeline. Each
runtime is an independent package under `runtimes/`.

## What gets published

Every runtime is built and scanned, then ships as **one container image and, where it fits, a
layer — both from the same payload**:

|  | Lambda layer | Container image |
| --- | --- | --- |
| **Runtimes** | the 7 that fit the 250 MB unzipped limit | all 12 |
| **Location** | `s3://tartarus-runtimes-us-east-1/<runtime>/<arch>/<sha256>.zip` | `ghcr.io/tartaruscode/lambda-runtime-factory/<runtime>` and `public.ecr.aws/tartaruscode/lambda-runtime-factory/<runtime>` |
| **Architectures** | `x86_64` and `arm64`, separate layers | one multi-arch manifest |
| **Address by** | layer name = runtime id (`pypy311`), compatible with `provided.al2023` | `<version>` (`1.26.10`), `<line>` (`1.26`), `latest` |

- Uploads are content-addressed, so a layer version always points at the exact bytes it was built
  from, and layers are public (`lambda:GetLayerVersion` to `*`).
- **Five runtimes ship no layer**: `go126`, `go127`, `rust194`, `rust198` and `rust199` carry a
  whole upstream toolchain (`layer_publishable: false`). They are still built and scanned, and they
  publish as images. See [DELIVERY.md](DELIVERY.md).
- **The base is Iron Bank UBI9** — `registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal`, the
  DoD-hardened minimal userland, for every runtime. Each image runs as non-root `USER 1001:1001` on a
  read-only rootfs with `/tmp` as the only writable path, carries no package manager, and bakes any
  `certs/*.crt` root CA into its trust store. A build without Iron Bank credentials falls back to
  `registry.access.redhat.com/ubi9/ubi-minimal`, which serves the same userland.
- **`-soci` tags** — the same image with a SOCI index bundled in, for functions on **Lambda Managed
  Instances**, which then lazy-load it instead of downloading the whole thing. There is no moving
  `-soci` tag: that image is immutable by design.

Resolve the current ARN for a layer:

```bash
aws lambda list-layer-versions \
  --layer-name pypy311 --region us-east-1 \
  --query 'LayerVersions[0].LayerVersionArn' --output text

make latest RUNTIME=pypy311     # or: make latest-all
```

Attach it to a function:

```bash
aws lambda update-function-configuration --function-name my-function \
  --layers arn:aws:lambda:us-east-1:485809183296:layer:pypy311:<version>
```

The layers live in AWS account `485809183296`; `make latest` prints the full ARN.

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

The entrypoint is `/opt/bootstrap`, the same file the zip layer ships, so the handler contract is
identical either way: `CMD` carries the handler name and the runtime reads it exactly as it reads
`_HANDLER`. Any `certs/*.crt` in this repository is baked into the image's trust store, and
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
| `go126` | Go 1.26 | 1.26 | Go toolchain layer |
| `go127` | Go 1.27 | 1.27 | Go toolchain layer |
| `graalpy312` | GraalPy 3.12 | 3.12 | GraalPy on Python, with the layer's site-packages on `sys.path` |
| `graalpy313` | GraalPy 3.13 | 3.13 | GraalPy on Python, with the layer's site-packages on `sys.path` |
| `pypy311` | PyPy 3.11 | 3.11 | Portable PyPy build from python.org; ships the Runtime API bootstrap and the helper package |
| `pypy312` | PyPy 3.12 | 3.12 | Portable PyPy build from python.org; ships the Runtime API bootstrap and the helper package |
| `rust194` | Rust 1.96 | 1.96 | Rust musl toolchain layer |
| `rust198` | Rust 1.98 | 1.98 | Rust musl toolchain layer |
| `rust199` | Rust 1.99 | 1.99 | Rust musl toolchain layer |


Ids follow the framework and version line: a runtime tracks one line (`go126` is Go 1.26), and a
new line becomes a new runtime rather than mutating an existing one. Run `make list-runtimes` for
the ids the current matrix builds.

## Repository layout

```text
runtimes/<runtime-id>/   runtime.json, bootstrap/, helpers/, checksums/, examples/
tools/bin/               build, audit, upload, publish, local-test entrypoints
tools/runtime_lib/       family defaults, manifest resolution, bump tooling, tests
tools/docker/            Dockerfile.ironbank — the canonical Iron Bank stage layout
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
