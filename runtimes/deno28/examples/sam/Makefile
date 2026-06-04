RUNTIME := deno28

package:
	sam package \
		--template-file template.yml \
		--output-template-file packaged.yml \
		--s3-bucket deno-examples-us-east-1 \
		--region us-east-1

deploy: package
	aws cloudformation deploy \
		--template-file packaged.yml \
		--stack-name deno-example-sam \
		--capabilities CAPABILITY_IAM \
		--parameter-overrides DenoLayerArn=$$DENO_LAYER_ARN \
		--region us-east-1

local-build:
	$(MAKE) -C ../../../.. local-build RUNTIME=$(RUNTIME)

local-invoke:
	$(MAKE) -C ../../../.. local-invoke RUNTIME=$(RUNTIME)
