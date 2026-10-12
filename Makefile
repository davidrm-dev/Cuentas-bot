STACK ?= cuentas-bot
REGION ?= us-east-1

.PHONY: test lint build deploy web telegram all

test:
	python -m pytest -q

lint:
	cfn-lint template.yaml

build:
	sam build

deploy: build
	sam deploy --stack-name $(STACK) --region $(REGION) --capabilities CAPABILITY_IAM \
		--resolve-s3 --no-fail-on-empty-changeset

# Sube la Mini App al bucket y limpia la caché de CloudFront.
web:
	$(eval BUCKET := $(shell aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='WebBucketName'].OutputValue" --output text))
	$(eval DIST := $(shell aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text))
	aws s3 sync webapp/ s3://$(BUCKET)/ --delete
	aws cloudfront create-invalidation --distribution-id $(DIST) --paths "/*" > /dev/null

telegram:
	python scripts/setup_telegram.py --stack $(STACK) --region $(REGION)

all: deploy web telegram
