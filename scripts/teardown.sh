#!/usr/bin/env bash
# Remove every AvionPulse resource (stops all charges). Irreversible: the table and bucket are deleted.
set -euo pipefail
STACK_NAME="${STACK_NAME:-avionpulse}"
REGION="${AWS_REGION:-us-east-1}"

BUCKET=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text || true)
if [[ -n "$BUCKET" && "$BUCKET" != "None" ]]; then
  echo "==> Emptying s3://$BUCKET"
  aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" >/dev/null
fi

echo "==> Deleting stack $STACK_NAME (waits until done, ~5-15 min: CloudFront is slow to delete)"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"

if [[ "${KEEP_API_KEY:-}" != "1" ]]; then
  aws ssm delete-parameter --region "$REGION" --name "/${STACK_NAME}/datadog/api-key" 2>/dev/null || true
fi
echo "Done. Nothing from AvionPulse is left running."
echo "Left behind on purpose (cents/month at most): the code artifact bucket (${STACK_NAME}-artifacts-*) if you used deploy-cfn,"
echo "and the SAM-managed bucket (aws-sam-cli-managed-default-*) if you used the SAM deploy. Delete them in the S3 console if unused."
