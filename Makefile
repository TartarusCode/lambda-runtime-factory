SHELL=/bin/bash

RUNTIME ?= pypy311
ARCH ?= x86_64
ARCHES := x86_64 arm64
RUNTIMES := $(shell python3 tools/runtime_lib/runtime_manifest.py list)
LOCAL_AWS_ENV := env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_SESSION_TOKEN=test AWS_REGION=us-east-1
DEV_BUILD_ENV := BUILD_BEST_EFFORT_AUDIT=0

all: validate-runtimes build

.PHONY: all list-runtimes validate-runtimes check build build-all build-all-arches audit audit-all upload upload-all publish publish-all publicize publicize-all latest latest-all unpublish create-buckets local-build local-invoke clean shell check-updates bump bump-latest test

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

check-updates:
	bash tools/bin/bump-runtime check

bump:
	bash tools/bin/bump-runtime bump "$(RUNTIME)" "$(VERSION)"

bump-latest:
	bash tools/bin/bump-runtime bump-latest

clean:
	bash tools/bin/clean-runtime "$(RUNTIME)"

shell:
	docker run --rm -v "${PWD}":/opt public.ecr.aws/sam/build-provided.al2023 sh
