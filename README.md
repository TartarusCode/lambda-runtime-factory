# lambda-runtime-factory

AWS Lambda **runtime packages** for the `provided.al2023` runtime — a Lambda layer and a
distroless OCI image per runtime, built and published from one manifest-driven pipeline. Each
runtime is an independent package under `runtimes/`.

## What gets published

Every runtime in the matrix is built and scanned. Each runtime then ships as **two artifacts
from the same payload**:

**Lambda zip layer** — for the runtimes that fit Lambda's 250 MB unzipped deployment-package
limit:

- **Layer name** is the runtime id (`pypy311`, `deno29`, `graalpy312`, …)
- **Compatible runtime**: `provided.al2023`
- **Architectures**: `x86_64` and `arm64`
- **Regions**: `us-east-1`
- **Uploads** go to `s3://tartarus-runtimes-<region>/<runtime-id>/<arch>/<sha256>.zip` —
  content-addressed, so a layer version always points at the exact bytes it was built from
- **Layers are public**: `lambda:GetLayerVersion` is granted to `*`
- **Five runtimes are built but not published as layers** — `go126`, `go127`, `rust194`,
  `rust198` and `rust199` ship a whole upstream toolchain and are flagged
  `layer_publishable: false`. CI keeps building and scanning them; they publish as images.
  See [DELIVERY.md](DELIVERY.md) for the constraint.

**OCI image** — for **every** runtime, including the toolchains a layer cannot carry:

- **Images**: `ghcr.io/tartaruscode/lambda-runtime-factory/<runtime-id>` and
  `public.ecr.aws/tartaruscode/lambda-runtime-factory/<runtime-id>` — GHCR for browsing, ECR
  Public because a Lambda function cannot pull from GHCR at execution time and gets anonymous,
  high-bandwidth pulls from ECR inside AWS. The ECR path needs `tartaruscode` to be an active
  alias on the registry (the account's default alias is `tartarusgroup`; a custom alias is
  requested in the console, and either one pulls fine)
- **Architectures**: one multi-arch manifest for `linux/amd64` and `linux/arm64`
- **Tags**: `<distribution-version>` (e.g. `1.26.10`), the version line (`1.26`), and
  `latest` on a tagged release
- **SOCI copies**: `<distribution-version>-soci` and `<line>-soci` are the same images with a
  SOCI index bundled in. Point a function that runs on **Lambda Managed Instances** at the
  `-soci` tag and Lambda lazy-loads the image instead of downloading all of it first. There is no
  moving `-soci` tag: that image is immutable by design.
- **Debug variants**: every distroless runtime also publishes `<version>-debug`,
  `<line>-debug` and `debug` — the same payload and entrypoint on a base that carries a
  busybox shell, for inspecting an image that failed to start. The production tags never
  have a shell.
- **SOCI indexes**: the release publishes a SOCI index for the ECR Public copy, so Lambda
  can start the image lazily instead of downloading every layer first
- **Two shapes**, because a runtime base and a build environment are different things:
  - **Runtime base** (the interpreter runtimes) — `gcr.io/distroless/cc-debian12`: no shell,
    no package manager, minimal OS CVE surface, and a **10 GB** uncompressed limit instead of
    the 250 MB layer budget. The entrypoint is exec-form and shell-free: the runtime binary,
    or the runtime's compiled bootstrap launcher. `aws-lambda-rie` is mounted only for the
    smoke test, never shipped.
  - **Build environment** (the Go and Rust toolchains) — `debian:12-slim` and `alpine:3.22`
    respectively, with a shell and a package manager, because the documented use is a
    Dockerfile build stage and a `RUN` step needs a shell. The toolchain is on `PATH`; the
    image's smoke test runs it (`go version`, `rustc --version`).

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

The image is distroless, so anything installed with `RUN` has to happen in a builder stage —
see [examples/docker](examples/docker) for recipes covering pure-Python dependencies, a Bun
bundle and the Go toolchain.

When an image fails to start, use the debug tag: the same payload and entrypoint with a
busybox shell, so you can look at the real container instead of guessing.

```bash
docker run --rm -it --entrypoint /busybox/sh \
  ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11-debug
```

The Go and Rust images are **toolchain images**: they carry `go`/`rustc`, not a handler, and
they are build environments rather than runtime bases — `debian:12-slim` and `alpine:3.22`
(musl), so a `RUN` step has a shell to run in. Use one as a build stage and deliver the
compiled bootstrap on the OS-only base:

```dockerfile
FROM ghcr.io/tartaruscode/lambda-runtime-factory/go126:1.26 AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/bootstrap .

FROM public.ecr.aws/lambda/provided:al2023
COPY --from=build /out/bootstrap ${LAMBDA_RUNTIME_DIR}/bootstrap
```

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
.github/workflows/       CI, release, audit and weekly version-bump pipelines
.github/actions/         shared composite actions: matrix, build environment, build + audit
```

## Development

Building, testing, releasing and extending the packages — the build environment, every `make`
target, the local SAM flow, the CI and release pipelines, and how to add a runtime or a version
line — is documented in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache 2.0
