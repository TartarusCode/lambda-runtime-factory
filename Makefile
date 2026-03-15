SHELL=/bin/bash

RUNTIME ?= pypy311
RUNTIMES := $(shell python3 tools/runtime_lib/runtime_manifest.py list)
LOCAL_AWS_ENV := env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_SESSION_TOKEN=test AWS_REGION=us-east-1
DEV_BUILD_ENV := BUILD_BEST_EFFORT_AUDIT=0

all: validate-runtimes build

.PHONY: all list-runtimes validate-runtimes check build build-all audit audit-all upload upload-all publish publish-all publicize publicize-all latest latest-all unpublish create-buckets local-build local-invoke clean shell

list-runtimes:
	python3 tools/runtime_lib/runtime_manifest.py list

validate-runtimes:
	bash tools/bin/validate-runtimes

check:
	bash tools/bin/check-runtime "$(RUNTIME)"

build: validate-runtimes
	$(DEV_BUILD_ENV) bash tools/bin/build-runtime "$(RUNTIME)"

build-all: validate-runtimes
	@for runtime in $(RUNTIMES); do \
		$(DEV_BUILD_ENV) bash tools/bin/build-runtime "$$runtime"; \
	done

audit:
	bash tools/bin/audit-runtime "$(RUNTIME)"

audit-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/audit-runtime "$$runtime"; \
	done

upload:
	bash tools/bin/upload-runtime "$(RUNTIME)"

upload-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/upload-runtime "$$runtime"; \
	done

publish:
	bash tools/bin/publish-runtime "$(RUNTIME)"

publish-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/publish-runtime "$$runtime"; \
	done

publicize:
	bash tools/bin/publish-runtime --publicize "$(RUNTIME)"

publicize-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/publish-runtime --publicize "$$runtime"; \
	done

latest:
	bash tools/bin/latest-runtime "$(RUNTIME)"

latest-all:
	@for runtime in $(RUNTIMES); do \
		bash tools/bin/latest-runtime "$$runtime"; \
	done

unpublish:
	bash tools/bin/unpublish-runtime "$(VERSION)" "$(RUNTIME)"

create-buckets:
	bash tools/bin/create-buckets "$(RUNTIME)"

local-build:
	$(DEV_BUILD_ENV) bash tools/bin/local-build-runtime "$(RUNTIME)"

local-invoke:
	$(LOCAL_AWS_ENV) $(DEV_BUILD_ENV) bash tools/bin/local-invoke-runtime "$(RUNTIME)"

clean:
	bash tools/bin/clean-runtime "$(RUNTIME)"

shell:
	docker run --rm -v "${PWD}":/opt public.ecr.aws/sam/build-provided.al2023 sh
