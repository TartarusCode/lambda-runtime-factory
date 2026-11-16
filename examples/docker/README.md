# Container examples

Recipes for consuming the published runtime images. All of them follow one
contract, so a function image is `FROM <runtime>` + `COPY` + `CMD`:

- the image's `ENTRYPOINT` is `/opt/bootstrap`, which applies the Lambda
  filesystem contract and execs the runtime;
- `CMD` names the handler, exactly as `_HANDLER` would. The runtime reads
  whichever is present, so a function image never has to set an environment
  variable.

Every runtime image is the same shape: one image per runtime, on the DoD Iron Bank
`ubi9-minimal` base, published as a multi-arch manifest. It runs as
`USER 1001:1001` with `WORKDIR /var/task`, the rootfs is mounted read-only at run
time with `/tmp` as the only writable path, and no package manager is left in the
image. `/opt` and `/var/task` are therefore read-only at run time: anything you
need to install belongs in a builder stage, not in the runtime stage. The RPM
database stays on purpose — it is what lets Syft and Grype attribute the base's
packages.

| Recipe | Shows |
| --- | --- |
| `python-pure-deps/` | dependencies installed in a `python:3.11-slim` builder and copied in |
| `go-build-stage/` | the Go toolchain image as a build stage, a static binary on an OS-only base |
| `bun-deps-bundle/` | `bun build` produces one file, so no `node_modules` ships |

The recipes are templates: copy one into your project and put your own `app.py` /
`main.go` / `app.ts` next to it.

## Tags and variants

`:<version>` (`1.26.10`) is the immutable, multi-arch image: the tag a release
creates and the one to pin. `:<line>` (`1.26`) tracks the line, and `:latest`
moves on tagged releases. There is one image per runtime, so there is no variant
suffix on the standard image.

`:<version>-soci` and `:<line>-soci` are the SOCI-enabled copies — the same
payload with a SOCI index bundled in, for functions on **Lambda Managed
Instances**, which lazy-load it instead of pulling every layer. They are separate
images with their own digests, there is no moving `-soci` tag, and the
`<version>-soci` tag is what to pin.

## Looking inside an image

The base is UBI9-minimal, so the image carries a shell. Run it as the runtime user
and inspect the payload in place:

```bash
docker run --rm -it --entrypoint /bin/sh \
  ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11
# then: ls /var/task, ls /opt/pypy/site-packages, /opt/pypy/bin/pypy3 -c 'import x'
```

`/opt` is world-readable (`chmod -R a+rX`), so UID 1001 can read what it needs
without escalating. Pass `--user 0` only for a local probe you throw away.

## C extensions on the Python runtimes

The Python payloads are slimmed: no C headers, no `ensurepip`, no stdlib test
suites, no GUI toolkits. Pure-Python dependencies copy in fine (see
`python-pure-deps/`). A package with a C extension has to be built against the
*same interpreter* (PyPy and GraalPy are not CPython, and CPython wheels do not
load in them) and needs headers the payload does not carry, so build it in a stage
that downloads the same upstream distribution and compiles with that interpreter's
own tooling, then copy the result into the runtime image.