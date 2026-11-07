# syntax=docker/dockerfile:1
#
# Canonical Iron Bank hardened image for a lambda-runtime-factory runtime.
#
# This file is the single source of the Iron Bank stage layout. It is a template:
# tools/runtime_lib/container_image.py renders it per runtime, substituting the
# @@...@@ placeholders with the labels and the payload path the runtime manifest
# declares. Keeping the layout here (rather than in Python string concatenation)
# is what makes the STIG invariants reviewable:
#
#   * Stage 1 (builder) injects the DoD/enterprise root CAs and extracts the
#     trust store.
#   * Stage 2 (runtime) is the shipped image. Library dependencies are installed
#     with the base's own package manager — never by copying resolved files —
#     so `/var/lib/rpm` and `/etc/ld.so.cache` stay consistent and Grype's RHEL
#     backport detection keeps working. The package manager is removed again
#     before the image ships. The process runs as UID 1001 on a read-only rootfs
#     with /tmp as the only writable path.
#
# The base is registry1.dso.mil (Iron Bank). A build without Iron Bank
# credentials falls back to registry.access.redhat.com, which serves the same
# UBI9-minimal userland; the release always builds from the primary base.
ARG BASE_IMAGE="registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest"
ARG BUILDER_IMAGE="${BASE_IMAGE}"

# ---------------------------------------------------------------------------
# Stage 1: certificate hardening
# ---------------------------------------------------------------------------
FROM ${BUILDER_IMAGE} AS builder
USER 0
WORKDIR /build

# Inject custom DoD or enterprise root CAs if provided. The directory is always
# present in the build context (build-container creates it), so this COPY cannot
# fail on a runtime that ships no extra CAs.
COPY certs/ /etc/pki/ca-trust/source/anchors/
RUN if [ -d /etc/pki/ca-trust/source/anchors ] && [ "$(ls -A /etc/pki/ca-trust/source/anchors 2>/dev/null)" ]; then \
        update-ca-trust extract; \
    fi

# ---------------------------------------------------------------------------
# Stage 2: hardened STIG rootless runtime
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime
ARG NONROOT_UID=1001
ARG NONROOT_GID=1001

@@LABELS@@

# The extracted trust store, with the injected CAs baked in.
COPY --from=builder /etc/pki/ca-trust/extracted /etc/pki/ca-trust/extracted

# Every TLS stack in the payload reads the same bundle, so a DoD CA is trusted by
# Python, Node, Deno and anything linking OpenSSL without per-runtime config.
ENV SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    DENO_CERT=/etc/pki/tls/certs/ca-bundle.crt \
    NODE_EXTRA_CA_CERTS=/etc/pki/tls/certs/ca-bundle.crt

# The layer payload lives at /opt, exactly as Lambda mounts a layer — and it is
# the same ${LAYER_ROOT} tree build-runtime produced, owned by the runtime UID so
# a read-only mount is the only thing standing between the function and its own
# payload.
COPY --chown=${NONROOT_UID}:${NONROOT_GID} layer/ @@LAYER_ROOT@@/

# STIG: every file readable and every directory traversable by any UID, because
# Lambda runs the function under a UID the build never saw.
RUN chmod -R a+rX @@LAYER_ROOT@@

# Runtime library dependencies, installed by the base's own package manager so
# RPM's file database and the dynamic-linker cache are maintained by RPM itself.
# Copying resolved `.so` files by hand would leave `/var/lib/rpm` without the
# owning package (Grype then cannot match a RHEL backport and reports false
# positives) and `/etc/ld.so.cache` stale.
ARG STAGE_PACKAGES=""
RUN if [ -n "${STAGE_PACKAGES}" ]; then \
        microdnf install -y --nodocs --setopt=install_weak_deps=0 ${STAGE_PACKAGES} && \
        microdnf clean all && \
        rm -rf /var/cache/dnf /var/log/*; \
    fi

# A runtime image has no use for a package manager, and STIG wants none. Removing
# only packages that are actually installed keeps the transaction from aborting on
# a name the base does not carry (the fallback base has no `dnf`, and one unknown
# package fails the whole `microdnf remove`). rpm is kept: it owns the rpmdb that
# vulnerability scanners read.
RUN for pkg in microdnf dnf yum; do \
        if rpm -q "${pkg}" >/dev/null 2>&1; then microdnf remove -y "${pkg}" || true; fi; \
    done; \
    command -v microdnf >/dev/null 2>&1 && microdnf clean all || true; \
    rm -f /usr/bin/microdnf /usr/bin/dnf /usr/bin/yum; \
    rm -rf /var/cache/dnf /var/cache/yum /var/lib/dnf

# Syft and Grype locate RHEL backports through the RPM database, and identify the
# distribution at all through /etc/os-release. An image that has lost either still
# builds and still runs — it just produces an SBOM with no lineage, which is the
# compliance artifact this image exists for. Assert both here, where the cause is
# still visible, instead of discovering it in a scan report.
RUN set -eu; \
    rpmdb=""; \
    for candidate in /var/lib/rpm /usr/lib/sysimage/rpm; do \
        if [ -d "${candidate}" ] && [ -n "$(ls -A "${candidate}" 2>/dev/null)" ]; then \
            rpmdb="${candidate}"; \
        fi; \
    done; \
    if [ -z "${rpmdb}" ]; then \
        echo "no RPM database under /var/lib/rpm or /usr/lib/sysimage/rpm:" \
             "Syft and Grype would lose RHEL backport lineage" >&2; \
        exit 1; \
    fi; \
    if [ ! -s /etc/os-release ]; then \
        echo "/etc/os-release is missing, so a scanner cannot identify the distribution" >&2; \
        exit 1; \
    fi; \
    echo "RPM lineage: ${rpmdb} ($(ls "${rpmdb}" | wc -l) entries), os-release present"

@@ENV@@

WORKDIR @@WORKDIR@@
USER ${NONROOT_UID}:${NONROOT_GID}

# Exec form only: the payload's bootstrap applies the Lambda filesystem contract
# and execs the runtime. `/opt` and `/var/task` are read-only at run time, so
# every path the runtime writes (HOME, TMPDIR, GOCACHE, …) points under /tmp.
ENTRYPOINT ["/opt/bootstrap"]
