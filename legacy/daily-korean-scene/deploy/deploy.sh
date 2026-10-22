#!/usr/bin/env bash
# Build and deploy Daily Korean Scene to AWS Lambda (Function URLs) with SAM.
# Creates billable resources (2 Lambda functions, 1 DynamoDB table) - run only when you mean to deploy.
#
#   AWS_PROFILE=dev1 REGION=us-east-1 bash deploy/deploy.sh
#
# Needs: AWS SAM CLI, pip, zip. The build installs arm64 (manylinux aarch64) wheels.
set -euo pipefail
cd "$(dirname "$0")/.."
REGION=${REGION:-us-east-1}
STACK=${STACK:-daily-korean-scene}
LWA_LAYER=${LWA_LAYER:-arn:aws:lambda:${REGION}:753240598075:layer:LambdaAdapterLayerArm64:25}
CONTENT=${CONTENT:-../content}

rm -rf build/lambda && mkdir -p build/lambda
pip install --quiet --target build/lambda --platform manylinux2014_aarch64 --implementation cp \
  --python-version 3.12 --only-binary=:all: -r requirements.txt
cp -r dks prompts simulator skill build/lambda/
mkdir -p build/lambda/content && cp -r "$CONTENT/scenes" build/lambda/content/
[ -d "$CONTENT/audio" ] && cp -r "$CONTENT/audio" build/lambda/content/
cp deploy/run_mcp.sh deploy/run_simulator.sh build/lambda/ && chmod +x build/lambda/*.sh
find build/lambda -name __pycache__ -prune -exec rm -rf {} +

deploy() {
  sam deploy --template-file deploy/template.yaml --stack-name "$STACK" --region "$REGION" \
    --capabilities CAPABILITY_IAM --resolve-s3 --no-confirm-changeset \
    --parameter-overrides "WebAdapterLayerArn=$LWA_LAYER" "PublicMcpBaseUrl=$1"
}
deploy ""
BASE=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='McpBaseUrl'].OutputValue" --output text)
deploy "${BASE%/}"   # second pass: absolute audio URLs and card CSP use the public MCP URL
aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" --query "Stacks[0].Outputs" --output table
