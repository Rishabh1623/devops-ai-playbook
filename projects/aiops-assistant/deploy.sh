#!/usr/bin/env bash
# =============================================================================
# AIOps Assistant — Deployment Script
#
# Kira runs as an agent loop in agent.py (Claude on Bedrock via the Converse
# API, calling the Lambdas as tools). Bedrock Agents (classic) is in
# maintenance mode and can't be created on new accounts, so there is no
# Bedrock Agent to deploy any more.
#
# What this script does:
#   - Verifies the 3 Lambda functions exist
#   - Sets their timeout to 30s
#
# What to do BEFORE running this script:
#   1. ./setup-iam.sh            (creates aiops-lambda-role)
#   2. Create 3 Lambda functions with code from lambda/ directory
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
# =============================================================================

set -euo pipefail

REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo ""
echo "============================================="
echo " AIOps — Deployment"
echo " Account : $ACCOUNT_ID"
echo " Region  : $REGION"
echo "============================================="
echo ""

# =============================================================================
# STEP 1: Verify Lambda functions exist
# =============================================================================
echo "[1/2] Pre-flight checks..."

for FUNC in aiops-fetch-logs aiops-fetch-metrics aiops-fetch-health; do
  if ! aws lambda get-function --function-name "$FUNC" --region "$REGION" &>/dev/null; then
    echo "  ✗ Lambda '$FUNC' not found in $REGION"
    echo "    Create it on the AWS Console first, then re-run this script."
    exit 1
  fi
  echo "  ✓ Lambda: $FUNC"
done

# =============================================================================
# STEP 2: Update Lambda timeouts
# =============================================================================
echo ""
echo "[2/2] Configuring Lambda functions..."

for FUNC in aiops-fetch-logs aiops-fetch-metrics aiops-fetch-health; do
  aws lambda update-function-configuration \
    --function-name "$FUNC" \
    --timeout 30 \
    --region "$REGION" \
    --query 'FunctionName' --output text > /dev/null
  echo "  ✓ $FUNC timeout set to 30s"
done

echo ""
echo "============================================="
echo " Done!"
echo "============================================="
echo ""
echo " Next steps:"
echo "  1. Generate sample data:"
echo "     python3 scripts/generate_sample_data.py --region $REGION"
echo ""
echo "  2. Run the Streamlit UI:"
echo "     cp .env.example .env      # optional: set BEDROCK_MODEL_ID"
echo "     python3 -m venv venv && . venv/bin/activate"
echo "     pip install -r requirements.txt"
echo "     streamlit run app.py"
echo ""
echo "  The AWS identity running app.py needs bedrock:InvokeModel on the model's"
echo "  inference profile and lambda:InvokeFunction on the 3 aiops-* functions."
echo ""
