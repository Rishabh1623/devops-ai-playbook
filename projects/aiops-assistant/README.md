# AIOps Assistant — Kira

An AI-powered SRE assistant built on Claude (Amazon Bedrock). Kira diagnoses production incidents by querying CloudWatch Logs, Prometheus metrics, and EKS cluster health — then responds with root cause, evidence, and fix recommendations.

---

## Architecture

```
Streamlit UI (app.py)
      │
      ▼
Kira agent loop (agent.py) ──► Claude on Bedrock (Converse API, tool use)
      │  invokes the Lambda for each tool Claude asks for
      ├── fetch_logs         → CloudWatch Logs
      ├── fetch_metrics      → Prometheus (ELB endpoint)
      └── fetch_service_health → EKS cluster + node groups
```

> **Why not a Bedrock Agent?** Bedrock Agents (classic) is in maintenance mode and new agent creation is blocked on accounts without prior usage. Kira now runs its own agent loop in `agent.py`: it sends the question and the 3 tool definitions (built from `schemas/*.json`) to Claude, invokes the matching Lambda whenever Claude requests a tool, and feeds the result back until Claude answers. The Lambdas receive the same event format a Bedrock Agent would send, so their code is unchanged.

---

## Prerequisites

- AWS account with access to a Claude model on Bedrock (default: `us.anthropic.claude-sonnet-4-6`)
- EKS cluster running with Prometheus exposed via a LoadBalancer service
- AWS CLI configured (`aws configure`)
- Python 3.10+

---

## Step 1: Set Up IAM Roles

Run the provided script to create the Lambda execution role:

```bash
chmod +x setup-iam.sh
./setup-iam.sh
```

This creates:

| Role | Used By | Permissions |
|------|---------|-------------|
| `aiops-lambda-role` | All 3 Lambda functions | CloudWatch Logs read, EKS describe, Lambda basic execution |

The AWS identity that runs `app.py` (your CLI user/role) separately needs `bedrock:InvokeModel` on the Claude inference profile and `lambda:InvokeFunction` on the 3 `aiops-*` functions.

---

## Step 2: Create the Lambda Functions

Create the following 3 Lambda functions in the AWS Console (or via CLI). Use the code from the `lambda/` directory.

| Function Name | Code File | Execution Role |
|---------------|-----------|----------------|
| `aiops-fetch-logs` | `lambda/fetch_logs/lambda_function.py` | `aiops-lambda-role` |
| `aiops-fetch-metrics` | `lambda/fetch_metrics/lambda_function.py` | `aiops-lambda-role` |
| `aiops-fetch-health` | `lambda/fetch_health/lambda_function.py` | `aiops-lambda-role` |

Runtime: **Python 3.12** | Timeout: **30 seconds**

---

## Step 3: Set the Prometheus URL

Both `fetch_metrics` and `fetch_health` lambdas query Prometheus directly. They read its address from the `PROMETHEUS_URL` environment variable, so the URL stays out of git. After creating the two functions, set it on each (or in the console: **Configuration → Environment variables**):

```bash
PROM=http://<YOUR_PROMETHEUS_ELB_URL>:9090
for fn in aiops-fetch-metrics aiops-fetch-health; do
  aws lambda update-function-configuration --function-name $fn \
    --environment "Variables={PROMETHEUS_URL=$PROM}" --region us-east-1
done
```

To get the Prometheus ELB URL, expose Prometheus as a LoadBalancer service:

```bash
kubectl patch svc kube-prometheus-stack-prometheus -n monitoring \
  -p '{"spec": {"type": "LoadBalancer"}}'

kubectl get svc kube-prometheus-stack-prometheus -n monitoring
# Copy the EXTERNAL-IP value — that is your ELB URL
```

---

## Step 4: Configure the Lambdas

Run the deploy script. It verifies the 3 Lambda functions exist and sets their timeout to 30s:

```bash
chmod +x deploy.sh
./deploy.sh
```

---

## Step 5: (Optional) Generate Sample Data

Populate CloudWatch Logs with realistic error scenarios to test Kira:

```bash
python3 scripts/generate_sample_data.py --region us-east-1
```

This writes 100 realistic log events (503 errors, OOM kills, connection pool exhaustion, etc.) to `/app/production`.

---

## Step 6: Run the Streamlit UI

```bash
cp .env.example .env
```

Edit `.env` if you need to change anything (all values are optional):

```env
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6

# Optional — omit to use your AWS CLI profile / SSO / IAM role:
# AWS_ACCESS_KEY_ID=<YOUR_ACCESS_KEY>
# AWS_SECRET_ACCESS_KEY=<YOUR_SECRET_KEY>
# AWS_SESSION_TOKEN=<YOUR_SESSION_TOKEN>
```

Install dependencies and start the UI:

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Open **http://localhost:8501** in your browser.

---

## Project Structure

```
aiops-assistant/
├── app.py                  # Streamlit chat UI
├── agent.py                # Kira agent loop (Claude + Lambda tools)
├── deploy.sh               # Lambda checks / configuration
├── setup-iam.sh            # Lambda IAM role and policies setup
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── lambda/
│   ├── fetch_logs/         # CloudWatch Logs query
│   ├── fetch_metrics/      # Prometheus metrics query
│   └── fetch_health/       # EKS cluster health check
├── schemas/
│   ├── fetch_logs.json     # Tool definition for fetch_logs (OpenAPI)
│   ├── fetch_metrics.json  # Tool definition for fetch_metrics (OpenAPI)
│   └── fetch_health.json   # Tool definition for fetch_health (OpenAPI)
└── scripts/
    └── generate_sample_data.py  # Seed CloudWatch with test errors
```

---

## Sample Questions to Ask Kira

- Why are we seeing 503 errors in the last hour?
- Is CPU usage high across the boutique services?
- Check database connections and latency
- Are all pods healthy? Any restarts?
- What are the most frequent errors in the last 2 hours?

---

## Potential Issues

### Claude model not available
If Kira replies with `AccessDeniedException ... is not available for this account`, your account can't use that model even if it appears in `aws bedrock list-foundation-models`. Test a model directly and set a working one as `BEDROCK_MODEL_ID` in `.env`:

```bash
aws bedrock-runtime converse --region us-east-1 \
  --model-id us.anthropic.claude-sonnet-4-6 \
  --messages '[{"role":"user","content":[{"text":"hi"}]}]' \
  --inference-config '{"maxTokens":10}'
```

### Prometheus URL unreachable from Lambda
`fetch_metrics` and `fetch_health` make outbound HTTP calls to the Prometheus ELB. If Lambda is deployed inside a VPC without a NAT gateway or internet gateway route, these calls will time out. Either:
- Keep Lambda outside a VPC (default), or
- Ensure the VPC has a route to the internet and the Prometheus ELB security group allows inbound on port 9090.

### fetch_logs returns no results
The default log group is `/eks/boutique/pods`. This group is only created after Fluent Bit starts shipping logs. Make sure `aws-for-fluent-bit` is running:

```bash
kubectl get pods -n amazon-cloudwatch
```

If the log group doesn't exist yet, run the sample data generator first (Step 5) which creates `/app/production`.

### fetch_health uses wrong cluster name
The Lambda defaults to cluster name `eks-cluster`. If your cluster has a different name, update `DEFAULT_CLUSTER` in `lambda/fetch_health/lambda_function.py` before uploading the function code.

### Lambda execution role missing permissions
If `fetch_health` returns an access denied error on `eks:DescribeCluster`, the inline policy may not have propagated yet (IAM can take ~10–15 seconds). Wait and retry. If it persists, verify the inline policy is attached:

```bash
aws iam get-role-policy \
  --role-name aiops-lambda-role \
  --policy-name aiops-lambda-inline-policy
```

### AWS credentials not resolving in Streamlit
If `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are left blank in `.env`, boto3 falls back to the default credential chain (`~/.aws/credentials`, environment variables, IAM role). If none of those are configured, Bedrock calls will fail with an auth error. Either fill in the credentials in `.env` or ensure your terminal session has valid AWS credentials before starting Streamlit.
