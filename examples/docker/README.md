# Container examples

Recipes for consuming the published runtime images. All of them follow one
contract, so a function image is `FROM <runtime>` + `COPY` + `CMD`:

- the image's `ENTRYPOINT` starts the runtime (distroless: no shell, non-root);
- `CMD` names the handler, exactly as `_HANDLER` would. The runtime reads
  whichever is present, so a function image never has to set an environment
  variable.

| Recipe | Shows |
| --- | --- |
| `python-pure-deps/` | dependencies installed in a `python:3.11-slim` builder and copied in |
| `go-build-stage/` | the Go toolchain image as a build stage, a static binary on an OS-only base |
| `bun-deps-bundle/` | `bun build` produces one file, so no `node_modules` ships |

The recipes are templates: copy one into your project and put your own `app.py` /
`main.go` / `app.ts` next to it.

## Image variants

`:<version>` is the production image: distroless, no shell, uid 65532.

`:<version>-debug` is the *same payload and entrypoint* on
`gcr.io/distroless/cc-debian13:debug-nonroot`, which also carries a busybox
shell. Use it when a handler fails with an import or `dlopen` error and you need
to look inside:

```bash
docker run --rm -it --entrypoint /busybox/sh \
  ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11-debug
# then: ls /var/task, ls /opt/pypy/site-packages, /opt/pypy/bin/pypy3 -c 'import x'
```

The debug image runs as the same uid with the same entrypoint, so what you
inspect is what failed — not a different container that happens to have a shell.
It is not meant to be deployed: it is the production image plus tools.

## C extensions on the Python runtimes

The Python payloads are slimmed: no C headers, no `pip`, no `ensurepip`.
Pure-Python dependencies copy in fine (see `python-pure-deps/`). A package with a
C extension has to be built against the *same interpreter* (PyPy and GraalPy are
not CPython, and CPython wheels do not load in them) and needs headers the
payload no longer carries, so build it in a stage that downloads the same
upstream distribution and compiles with that interpreter's own `pip`, then copy
the result into the runtime image.

A `-build` variant that carries `pip` plus the headers is the follow-up this
makes worth having: it would let a C extension be compiled directly against the
published payload, at roughly 15–20 MB of image size.
