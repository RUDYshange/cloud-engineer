#!/usr/bin/env bash
# Empty both buckets, then delete the stack.
# CloudFormation refuses to delete a bucket that still has objects in it.

set -euo pipefail

STACK="${1:-strongbox}"
REGION="${2:-$(aws configure get region || echo eu-west-1)}"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

for suffix in files web; do
  BUCKET="${STACK}-${suffix}-${ACCOUNT}-${REGION}"
  if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "==> Emptying s3://$BUCKET"
    aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" >/dev/null
  fi
done

echo "==> Deleting stack $STACK"
aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION"
echo "Gone. Nothing left billing."
