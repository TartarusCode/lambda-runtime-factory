# lambda-runtime-factory

AWS Lambda **layer** packages for the `provided.al2023` runtime, built and published from one
manifest-driven pipeline. Each runtime is an independent package under `runtimes/`.

## What gets published

Every runtime in the matrix is published as a Lambda layer, per architecture:

- **Layer name** is the runtime id (`pypy311`, `deno29`, `rust199`, …)
- **Compatible runtime**: `provided.al2023`
- **Architectures**: `x86_64` and `arm64`
- **Regions**: `eu-central-1`, `us-west-1`
- **Uploads** go to `s3://tartarus-runtimes-<region>/<runtime-id>/<arch>/<sha256>.zip` —
  content-addressed, so a layer version always points at the exact bytes it was built from
- **Layers are public**: `lambda:GetLayerVersion` is granted to `*`

Resolve the current ARN for a layer:

```bash
aws lambda list-layer-versions \
  --layer-name pypy311 --region eu-central-1 \
  --query 'LayerVersions[0].LayerVersionArn' --output text

make latest RUNTIME=pypy311     # or: make latest-all
```

Attach it to a function:

```bash
aws lambda update-function-configuration --function-name my-function \
  --layers arn:aws:lambda:eu-central-1:485809183296:layer:pypy311:<version>
```

The layers live in AWS account `485809183296`; `make latest` prints the full ARN.

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
