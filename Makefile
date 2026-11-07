SHELL=/bin/bash

RUNTIME ?= pypy311
ARCH ?= x86_64
ARCHES := x86_64 arm64
RUNTIMES := $(shell python3 tools/runtime_lib/runtime_manifest.py list)
LOCAL_AWS_ENV := env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_SESSION_TOKEN=test AWS_REGION=us-east-1
DEV_BUILD_ENV := BUILD_BEST_EFFORT_AUDIT=0

all: validate-runtimes build

.PHONY: all list-runtimes validate-runtimes check build build-all build-all-arches audit audit-all upload upload-all publish publish-all publicize publicize-all latest latest-all unpublish create-buckets local-build local-invoke clean shell check-updates bump bump-latest roll-off test container container-debug container-variants container-invoke container-audit container-push container-merge container-soci container-repos container-ironbank container-ironbank-invoke container-ironbank-audit

list-runtimes:
	python3 tools/runtime_lib/runtime_manifest.py list

validate-runtimes:
	bash tools/bin/validate-runtimes

test:
	python3 -m pytest tools/runtime_lib/tests -q

check:
	bash tools/bin/check-runtime "$(RUNTIME)" "$(ARCH)"

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

audit:
	bash tools/bin/audit-runtime "$(RUNTIME)" "$(ARCH)"

audit-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/audit-runtime "$$runtime" "$(ARCH)"; \
	done

upload:
	bash tools/bin/upload-runtime "$(RUNTIME)" "$(ARCH)"

upload-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/upload-runtime "$$runtime" "$(ARCH)"; \
	done

publish:
	bash tools/bin/publish-runtime "$(RUNTIME)" "$(ARCH)"

publish-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/publish-runtime "$$runtime" "$(ARCH)"; \
	done

publicize:
	bash tools/bin/publish-runtime --publicize "$(RUNTIME)" "$(ARCH)"

publicize-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/publish-runtime --publicize "$$runtime" "$(ARCH)"; \
	done

latest:
	bash tools/bin/latest-runtime "$(RUNTIME)" "$(ARCH)"

latest-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/latest-runtime "$$runtime" "$(ARCH)"; \
	done

unpublish:
	bash tools/bin/unpublish-runtime "$(VERSION)" "$(RUNTIME)" "$(ARCH)"

create-buckets:
	bash tools/bin/create-buckets "$(RUNTIME)" "$(ARCH)"

local-build:
	$(DEV_BUILD_ENV) bash tools/bin/local-build-runtime "$(RUNTIME)" "$(ARCH)"

local-invoke:
	$(LOCAL_AWS_ENV) $(DEV_BUILD_ENV) bash tools/bin/local-invoke-runtime "$(RUNTIME)" "$(ARCH)"

container: validate-runtimes
	$(DEV_BUILD_ENV) bash tools/bin/build-container "$(RUNTIME)" "$(ARCH)"

container-debug:
	$(DEV_BUILD_ENV) bash tools/bin/build-container "$(RUNTIME)" "$(ARCH)" --variant debug

container-variants:
	bash tools/bin/check-container-variants "$(RUNTIME)" "$(ARCH)"

container-merge:
	bash tools/bin/merge-container-tags "$(RUNTIME)" $(if $(SOCI),--soci)

# Converts the pushed per-arch image into a SOCI-enabled one. A dry run unless
# PUSH=1, because the result is a new image in the registry.
container-soci:
	bash tools/bin/publish-soci-index "$(RUNTIME)" "$(ARCH)" $(if $(PUSH),--push)

container-repos:
	bash tools/bin/ensure-container-repos "$(RUNTIME)"

container-audit:
	bash tools/bin/audit-container "$(RUNTIME)" "$(ARCH)"

container-invoke:
	bash tools/bin/local-invoke-container "$(RUNTIME)" "$(ARCH)"

# The Iron Bank (UBI9) image is a second artifact from the same payload: non-root
# UID 1001, DoD root CAs from certs/*.crt, and a read-only rootfs. Set
# USE_FALLBACK_UBI=1 to build from the Red Hat registry when registry1.dso.mil
# credentials are not available locally.
container-ironbank: validate-runtimes
	$(DEV_BUILD_ENV) bash tools/bin/build-container "$(RUNTIME)" "$(ARCH)" --ironbank

container-ironbank-invoke:
	bash tools/bin/local-invoke-container "$(RUNTIME)" "$(ARCH)" --ironbank

container-ironbank-audit:
	bash tools/bin/audit-container "$(RUNTIME)" "$(ARCH)" --ironbank

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

shell:
	docker run --rm -v "${PWD}":/opt public.ecr.aws/sam/build-provided.al2023 sh
