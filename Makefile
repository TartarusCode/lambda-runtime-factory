SHELL=/bin/bash

RUNTIME ?= pypy311
ARCH ?= x86_64
ARCHES := x86_64 arm64
RUNTIMES := $(shell python3 tools/runtime_lib/runtime_manifest.py list)
DEV_BUILD_ENV := BUILD_BEST_EFFORT_AUDIT=0

all: validate-runtimes build

.PHONY: all list-runtimes validate-runtimes check build build-all build-all-arches audit compliance attest test container container-check container-invoke container-audit container-push container-merge container-soci container-repos clean check-updates bump bump-latest roll-off

list-runtimes:
	python3 tools/runtime_lib/runtime_manifest.py list

validate-runtimes:
	bash tools/bin/validate-runtimes

test:
	python3 -m pytest tools/runtime_lib/tests -q

check:
	bash tools/bin/check-runtime "$(RUNTIME)" "$(ARCH)"

# Stage the payload: verify the upstream archive, extract, slim, record
# /opt/runtime-manifest.json and normalize the STIG permissions. Nothing is
# zipped — the image is the artifact.
build: validate-runtimes
	$(DEV_BUILD_ENV) bash tools/bin/build-runtime "$(RUNTIME)" "$(ARCH)"

build-all: validate-runtimes
	@for runtime in $(RUNTIMES); do \
		$(DEV_BUILD_ENV) bash tools/bin/build-runtime "$$runtime" "$(ARCH)"; \
	done

build-all-arches: validate-runtimes
	@for runtime in $(RUNTIMES); do \
		for arch in $(ARCHES); do \
			$(DEV_BUILD_ENV) bash tools/bin/build-runtime "$$runtime" "$$arch"; \
		done; \
	done

# SBOMs (SPDX + CycloneDX), a Grype report produced from that SPDX document, and
# the provenance manifest. `audit` is the historical name for the same thing.
compliance: validate-runtimes
	bash tools/bin/generate-compliance-artifacts "$(RUNTIME)" "$(ARCH)"

audit: compliance

# Keyless cosign signing and OCI artifact attachment. Needs an OIDC token, so this
# only works in CI unless a key is supplied.
attest:
	bash tools/bin/attest-container "$(RUNTIME)" "$(ARCH)"

container: validate-runtimes
	$(DEV_BUILD_ENV) bash tools/bin/build-container "$(RUNTIME)" "$(ARCH)"

# Proves the image's contract against the built bytes: non-root UID, the payload's
# entrypoint, no package manager, rpmdb intact.
container-check:
	bash tools/bin/check-container "$(RUNTIME)" "$(ARCH)"

container-merge:
	bash tools/bin/merge-container-tags "$(RUNTIME)" $(if $(SOCI),--soci)

# Converts the pushed per-arch image into a SOCI-enabled one. A dry run unless
# PUSH=1, because the result is a new image in the registry.
container-soci:
	bash tools/bin/publish-soci-index "$(RUNTIME)" "$(ARCH)" $(if $(PUSH),--push)

container-repos:
	bash tools/bin/ensure-container-repos "$(RUNTIME)"

container-audit: compliance

container-invoke:
	bash tools/bin/local-invoke-container "$(RUNTIME)" "$(ARCH)"

container-push:
	bash tools/bin/push-container "$(RUNTIME)" "$(ARCH)"

check-updates:
	bash tools/bin/bump-runtime check

bump:
	bash tools/bin/bump-runtime bump "$(RUNTIME)" "$(VERSION)"

bump-latest:
	bash tools/bin/bump-runtime bump-latest

roll-off:
	bash tools/bin/bump-runtime roll-off $(ARGS)

clean:
	bash tools/bin/clean-runtime "$(RUNTIME)"
