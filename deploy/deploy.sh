#!/usr/bin/env bash
# Build and deploy Family Relay (FR_PROFILE=aws) to AWS Lambda (Function URLs) with SAM. Creates billable
# resources (2 Lambda functions, 1 DynamoDB table) - run only when you mean to deploy.
#
#   AWS_PROFILE=<profile> REGION=eu-south-2 bash deploy/deploy.sh
# The speaker module (familyvoice, github.com/moduvoice/familyvoice) is bundled; browser (webm) audio needs ffmpeg in the
# function image - WAV input works without it.
set -euo pipefail
cd "$(dirname "$0")/.."
REGION=${REGION:-eu-south-2}
STACK=${STACK:-family-relay}
LWA_LAYER=${LWA_LAYER:-arn:aws:lambda:${REGION}:753240598075:layer:LambdaAdapterLayerArm64:25}

rm -rf build/lambda && mkdir -p build/lambda
pip install --quiet --target build/lambda --platform manylinux2014_aarch64 --implementation cp \
  --python-version 3.12 --only-binary=:all: -r requirements.txt -r requirements-voice.txt huggingface_hub
pip install --quiet --target build/lambda --no-deps "familyvoice @ git+https://github.com/moduvoice/familyvoice"
cp -r relay prompts simulator skill assets build/lambda/
cp deploy/run_mcp.sh deploy/run_simulator.sh build/lambda/ && chmod +x build/lambda/*.sh
find build/lambda -name __pycache__ -prune -exec rm -rf {} +

sam deploy --template-file deploy/template.yaml --stack-name "$STACK" --region "$REGION" \
  --capabilities CAPABILITY_IAM --resolve-s3 --no-confirm-changeset \
  --parameter-overrides "WebAdapterLayerArn=$LWA_LAYER"
aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" --query "Stacks[0].Outputs" --output table
