# lambda-runtime-factory

AWS Lambda **runtime packages** for the `provided.al2023` runtime — a Lambda layer and a
distroless OCI image per runtime, built and published from one manifest-driven pipeline. Each
runtime is an independent package under `runtimes/`.

## What gets published

Every runtime is built and scanned, then ships as **two artifacts from the same payload**:

|  | Lambda layer | Container image |
| --- | --- | --- |
| **Runtimes** | the 7 that fit the 250 MB unzipped limit | all 12 |
| **Location** | `s3://tartarus-runtimes-us-east-1/<runtime>/<arch>/<sha256>.zip` | `ghcr.io/tartaruscode/lambda-runtime-factory/<runtime>` and `public.ecr.aws/tartaruscode/lambda-runtime-factory/<runtime>` |
| **Architectures** | `x86_64` and `arm64`, separate layers | one multi-arch manifest |
| **Address by** | layer name = runtime id (`pypy311`), compatible with `provided.al2023` | `<version>` (`1.26.10`), `<line>` (`1.26`), `latest` |

- Uploads are content-addressed, so a layer version always points at the exact bytes it was built
  from, and layers are public (`lambda:GetLayerVersion` to `*`).
- **Five runtimes ship no layer**: `go126`, `go127`, `rust194`, `rust198` and `rust199` carry a
  whole upstream toolchain (`layer_publishable: false`). They are still built and scanned, and
  they publish as images. See [DELIVERY.md](DELIVERY.md).
- **`-debug` tags** — same payload and entrypoint on a base with a busybox shell, for looking at
  an image that failed to start. The production tags never contain a shell.
- **`-soci` tags** — the same image with a SOCI index bundled in, for functions on **Lambda
  Managed Instances**, which then lazy-load it instead of downloading the whole thing. There is no
  moving `-soci` tag: that image is immutable by design.
- **Bases** — interpreter runtimes use `gcr.io/distroless/cc-debian12` (no shell, minimal OS CVE
  surface, 10 GB limit instead of 250 MB); the Go and Rust toolchains use `debian:12-slim` and
  `alpine:3.22`, because a `RUN go build` in a builder stage needs a shell.

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
