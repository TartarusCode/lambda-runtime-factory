# Delivering runtime images with supply-chain evidence

Every runtime in this repository ships **one hardened OCI image** on the Iron Bank UBI9-minimal base,
plus the SBOMs, vulnerability reports and provenance a DoD continuous-ATO review asks for. There is no
Lambda layer and no `.zip`.

## Why there is no layer any more

In run `42121477502`, 12 of 24 publish cells failed `PublishLayerVersion`:

```
InvalidParameterValueException ... PublishLayerVersion:
Unzipped size must be smaller than 262144000 bytes
```

That is Lambda's deployment-package ceiling, quoted from the quotas page:

> **250 MB** — the maximum size of the contents of a deployment package, including layers and custom
> runtimes. (unzipped)

It counts unzipped bytes, it covers the function **and every attached layer** (so splitting a runtime
across layers does not help; a function may carry at most 5), and it is not adjustable.

After slimming, the numbers were: `graalpy312` 256,817,392 B (98.0% of the ceiling) and `graalpy313`
250,195,312 B (95.4%) — 3–11 MB left for the function's own code, against a ~225 MB
`libpythonvm.so` floor that no amount of pruning moves. `rust*` is 1.29 GB. So the ceiling was not a
tuning problem for six of the twelve runtimes then in the matrix; it was a wall.

`graalpy312` was dropped rather than carried into the image era. Upstream ended the GraalPy 3.12 line
at `graal-25.2.4`: every release from `graal-25.3.4` onward publishes only `graalpy3.13-*` assets, and
that 25.2.4 distribution bundles a JDK (`25.0.4+7-LTS-jvmci-25.2-b20`) with a fixable High CVE and no
newer 3.12 build to move to. `graalpy313` moved to `graal-25.5.5`, whose bundled JDK is
`25.0.5.0.3+1-LTS-jvmci-25.5-b26` — past the fix. Carrying an unpatched runtime and ignoring the CVE
is the one thing the compliance contract exists to prevent.

The second reason is worse. **A layer executes on AWS's `provided.al2023` base, not ours.** The STIG
user, the read-only rootfs, the injected trust store, the RPM-owned libraries and the absence of a
package manager are properties of the image — and a layer never sees any of them. Publishing both
meant publishing a compliance claim that only half the artifacts honoured.

## The published contract

```
ghcr.io/tartaruscode/lambda-runtime-factory/<id>:<version>
ghcr.io/tartaruscode/lambda-runtime-factory/<id>:<line>
ghcr.io/tartaruscode/lambda-runtime-factory/<id>:latest          # tagged releases only
```

The same tags are pushed to `public.ecr.aws/<alias>/lambda-runtime-factory/<id>` (the copy Lambda
can actually pull at execution time). Per architecture the tag is `<version>-<arch>`; the release
merges the two into one multi-arch manifest list. There is no variant suffix: one image per runtime,
on one base.

Each cell also publishes the **compliance bundle**:

| File | Standard |
| :--- | :--- |
| `<id>-<arch>.spdx.json` | SPDX 2.3 JSON — EO 14028, GitHub dependency graph |
| `<id>-<arch>.cdx.json` | CycloneDX JSON — DoD cATO, Dependency-Track |
| `<id>-<arch>.grype.json` | Grype vulnerability report, with RHEL backport disposition |
| `<id>-<arch>.grype.sarif` | SARIF for GitHub Security / CodeQL |
| `<id>-<arch>.provenance.json` | hardening + provenance attestation |

On every CI run they are uploaded with `actions/upload-artifact`. On a release they are also attached
to the pushed image digest and to the GitHub Release.

### Verifying a consumer's copy

```bash
# signature (keyless, GitHub Actions OIDC → Fulcio/Rekor)
cosign verify \
  --certificate-identity-regexp 'https://github.com/TartarusCode/lambda-runtime-factory/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11

# SBOMs attached to the digest
cosign download sbom ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11
oras discover ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11

# the provenance manifest, which names the base actually used
cosign verify-attestation --type slsaprovenance ... | jq .
```

`provenance.json` is the file to read when the question is "what is actually in this image": it
records the upstream archive URL and its verified SHA-256, the base image reference **and the digest
it resolved to**, the image digest, the unpacked size, the STIG checks and the CVE counts by severity.

## The stage layout

`tools/docker/Dockerfile` is the one canonical template, rendered per runtime by
`container_image.render_dockerfile`. Keeping the STIG invariants in a reviewable file rather than in
Python string concatenation is deliberate: a test reads the file, not the generator.

- **Stage 1 (`builder`)** copies `certs/*.crt` into `/etc/pki/ca-trust/source/anchors/` and runs
  `update-ca-trust extract`, so an injected DoD root CA is trusted by OpenSSL, Python, Node and Deno
  through the single bundle the final stage points every TLS stack at (`SSL_CERT_FILE`,
  `REQUESTS_CA_BUNDLE`, `DENO_CERT`, `NODE_EXTRA_CA_CERTS`).
- **Stage 2 (`runtime`)** copies the extracted trust store, copies the payload to `/opt` owned by the
  runtime UID, normalizes permissions (`chmod -R a+rX /opt`), installs the runtime's libraries with
  `microdnf`, then removes `/usr/bin/microdnf`, `/usr/bin/dnf` and `/usr/bin/yum`. It runs as
  `USER 1001:1001` with `WORKDIR /var/task` and `ENTRYPOINT ["/opt/bootstrap"]`.

### The RPM database is the product

Libraries are **installed as RPM packages, never copied as bare `.so` files**. That single decision is
what makes the rest of the compliance story possible:

- Syft reads the RPM database and produces a package-level SBOM with RHEL release/backport lineage,
  instead of a list of ELF files with no owner.
- Grype can then disposition a CVE against the vendor's patch stream — a library that Red Hat
  backported a fix into is *patched*, not vulnerable.
- `/etc/ld.so.cache` is whatever RPM says it is, rather than whatever the base happened to ship.

So `/var/lib/rpm` (or `/usr/lib/sysimage/rpm` on a newer layout) and `/etc/os-release` stay in the
image, and `verify_image_layout` fails a build that has lost either. The package manager binaries are
a different matter: a runtime image has no use for them and STIG wants none, so they are removed.

One soname genuinely cannot be satisfied: the Rust distribution is musl-linked, so its `rustc` needs
`libc.so` — musl's soname, which UBI9 has no package for (`musl-libc` is not in its repos, verified).
`container.unsatisfied_sonames` is empty repo-wide and `rust-musl` declares that one name, because the
binaries needing it are the ones the image's runtime contract does not run.

### The base falls back when credentials are absent

`registry1.dso.mil` needs a login, so a build without `IRONBANK_TOKEN` (an external PR, a fork, a
local run) uses `registry.access.redhat.com/ubi9/ubi-minimal`, which serves the same UBI9-minimal
userland. `BASE_IMAGE_DIGEST` pins the base when Iron Bank's daily rebuild would otherwise move it
under a reproducible build. The fallback is a build-time convenience, never a silent substitution:
the provenance manifest records which base was used.

## What is actually measured

Unpacked bytes summed from the exported rootfs, x86_64:

| Runtime | Unpacked |
| :--- | ---: |
| `pypy311` | 113,869,671 B |
| `bun13` | 116,422,312 B |
| `go127` | 286,425,568 B |
| `graalpy313` | 432,597,639 B (25.5.5) |
| `rust199` | 1,459,227,066 B |

The size is measured from the exported rootfs rather than `docker image inspect .Size`, because that
field is the *compressed* size on a containerd image store and the uncompressed one on the classic
overlay2 store: the same image read 82,283,585 B on a CI runner and 251,143,202 B on a developer box.

## Proving the contract rather than asserting it

`tools/bin/check-container` reads the contract back out of the built bytes — `USER`, `Entrypoint`,
`WorkingDir`, no package manager, rpmdb intact — and then runs a probe as that UID on a read-only
rootfs. `tools/bin/local-invoke-container` runs the smoke test under `--read-only`, a
`--tmpfs /tmp:rw,exec,size=512m,uid=1001,gid=1001` (the only writable path) and `--user 1001:1001`,
with `aws-lambda-rie` bind-mounted read-only from the host cache. The emulator is never baked into a
published image.

### Toolchain images have no handler to invoke

The smoke test for an image whose runtime has no handler is declared in the manifest
(`container.smoke_test`) and runs under the same rootless read-only flags:

- **Go** — `go version` must exit 0 and name the distribution version; Go's distribution is
  glibc-linked, so it runs on this base.
- **Rust** — `rustc` cannot run here at all (musl-linked, no musl loader), so the smoke test asserts
  the *entrypoint* contract instead: `/opt/bootstrap` with no handler exits 127 with the launcher's
  `_HANDLER is not set` diagnostic. A consumer's handler is a static musl binary at
  `/opt/rust/bin/<handler>`, which needs neither musl nor glibc.

## SOCI

`publish_soci: true` publishes a **second image per architecture** (`<version>-soci`, `<line>-soci`),
made by `soci convert --standalone` and merged like any other multi-arch image. Lambda lazy-loads the
image whose URI the function is given, and only on Lambda Managed Instances. It is opt-in because it
only pays off there. It needs no containerd: the conversion runs on an OCI layout that `oras` pulls
and pushes, filtered to one architecture so buildx's provenance attestation (`unknown/unknown`) cannot
reach `soci convert` — which otherwise fails with `soci: manifest should have proper platform`.

## What slimming prunes, and what it must not

Slimming removes stdlib test suites, GUI toolkits (`tkinter`, `turtledemo`, and the compiled
`_tkinter` module that pulled in X11/fontconfig/freetype), `ensurepip`, `idlelib`, `lib2to3`,
`pydoc_data`, C headers, static archives and external `.debug` scripts, then strips ELF debug symbols.
For Go it prunes the *top-level* `doc`, `test`, `api`, `misc` and `obj` directories only — `src/go/doc`
*is* the stdlib `go/doc` package and `go build` compiles the standard library from source — plus
`testdata` and `*_test.go` at any depth.

It never removes `*.dist-info`, `*.egg-info`, licence or version files: that metadata is how the SBOM
and the scan identify a component, and both run after the slimming, so they see exactly what ships.

## The bytecode experiment

Precompiling the interpreter's standard library to `.pyc` was expected to cut cold-start time.
Measured on `pypy311` x86_64, 12 cold starts per configuration:

| Payload | Unzipped | Init median | Init mean |
| :--- | ---: | ---: | ---: |
| sources only (default) | 90.3 MB | 1693 ms | 1675 ms |
| `PRECOMPILE_BYTECODE=1` | 106.5 MB | 1713 ms | 1744 ms |

It costs 16 MB and buys nothing measurable: the interpreter's own startup dominates this handler's
init. It stays an opt-in (`PRECOMPILE_BYTECODE=1`) for a family where imports are the bottleneck.

Source for the layer limits: <https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html>
