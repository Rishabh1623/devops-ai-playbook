#!/usr/bin/env bash
# =============================================================================
# AIOps Assistant — Deployment Script
#
# Kira runs as an agent loop in agent.py (Claude on Bedrock via the Converse
# API, calling the tools). Bedrock Agents (classic) is in
# maintenance mode and can't be created on new accounts, so there is no
# Bedrock Agent to deploy any more.
#
# fetch_metrics and fetch_service_health run inside the Kira process and
# query Prometheus privately, so only fetch_logs is a Lambda.
#
# What this script does:
#   - Verifies the aiops-fetch-logs Lambda exists
#   - Sets its timeout to 30s
#
# What to do BEFORE running this script:
#   1. ./setup-iam.sh            (creates aiops-lambda-role)
#   2. Create the aiops-fetch-logs Lambda with code from lambda/fetch_logs/
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

for FUNC in aiops-fetch-logs; do
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

for FUNC in aiops-fetch-logs; do
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
echo "  inference profile, lambda:InvokeFunction on aiops-fetch-logs, and"
echo "  eks:DescribeCluster/ListNodegroups/DescribeNodegroup. Run locally with"
echo "  kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090"
echo "  and PROMETHEUS_URL=http://localhost:9090."
echo ""
