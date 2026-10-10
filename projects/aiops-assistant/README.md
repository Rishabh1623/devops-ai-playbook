# Kira: an AI assistant for incidents

Kira helps with the first part of an incident: working out what is broken and why. You describe the symptom in a chat window, and Kira (Claude on AWS Bedrock) pulls logs, metrics, pod health and recent changes from the cluster, then answers with the likely root cause and the evidence. For simple fixes it can propose an action, which only runs after a person approves it.

Kira is Part 4 of the original series. The original README, written for a Bedrock Agent setup, is kept unchanged in [`ORIGINAL-README.md`](ORIGINAL-README.md). Bedrock Agents can no longer be created on new AWS accounts, so I rebuilt Kira as its own agent loop.

## How it works

```
Streamlit chat (app.py)
  → agent loop (agent.py) ⇄ Claude on Bedrock (Converse API)
      → runs the tools Claude asks for, sends the results back, repeats until Claude answers
```

| Tool | What it does | Runs in |
|------|--------------|---------|
| `fetch_logs` | Searches CloudWatch Logs | Lambda |
| `fetch_metrics` | Pod CPU, memory and restarts from Prometheus | Kira's pod |
| `fetch_service_health` | Cluster, node group and deployment health | Kira's pod |
| `fetch_recent_changes` | Kubernetes events, rollouts, Argo CD syncs and git commits | Kira's pod |
| `scale_deployment` | Sets a deployment's replica count (1–5) | Kira's pod, after approval |
| `restart_deployment` | Rolling restart | Kira's pod, after approval |
| `rollback_deployment` | Back to the previous version | Kira's pod, after approval |

The Prometheus tools run inside the cluster because Prometheus has no login and isn't exposed publicly.

## Safety

- **Tool output is treated as data.** Log lines can be written by any pod, so a log line could try to give Kira instructions. Every tool result is labelled as untrusted, and Kira is told never to follow instructions inside it.
- **Fixes need approval.** The chat pauses on an approval card showing the action, Kira's reason and what will change. Nothing runs until someone clicks Approve.
- **Allowlist in code.** Only scale, restart and roll back, only on the 7 shop deployments (not Kira itself or the database), checked when proposed and again before running. Kira's Kubernetes permissions are limited to the same deployments.
- **Cost limits.** A token budget per question (`KIRA_MAX_TOKENS_PER_QUESTION`, default 100000) and a rate limit (`KIRA_MAX_QUESTIONS_PER_WINDOW`, default 10 per 5 minutes).
- **Audit log.** Every proposed, approved, rejected and executed action is logged as JSON and shipped to CloudWatch (`"kira_action"`).

Kira's fixes are temporary: Argo CD deploys from git, so make the fix permanent with a commit.

## Run it

**On EKS** (how this repo runs it):

1. `terraform apply` in `projects/Infrastructure`. This creates Kira's AWS role (Bedrock, the logs Lambda, read-only EKS).
2. Create the logs Lambda: run `./setup-iam.sh`, create `aiops-fetch-logs` (Python 3.12) from `lambda/fetch_logs/lambda_function.py` with the `aiops-lambda-role` role, then run `./deploy.sh`.
3. Set the login password: `kubectl create secret generic kira-auth -n boutique --from-literal=APP_PASSWORD=<password>`
4. Push to `project-demo`. CI builds the image and Argo CD deploys it with `gitops/k8s/aiops-assistant/`. Open the `aiops-assistant` LoadBalancer address.

**Locally:**

```bash
cp .env.example .env          # optional: AWS_REGION, BEDROCK_MODEL_ID
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 &
export PROMETHEUS_URL=http://localhost:9090
python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt
streamlit run app.py          # http://localhost:8501
```

Locally Kira uses your AWS credentials and `~/.kube/config`.

Example questions: *"Why are we seeing 503 errors?"*, *"Are all pods healthy?"*, *"What changed in the last hour?"*

## Tests and drills

```bash
python -m unittest discover tests                    # no AWS needed
KIRA_LIVE_TESTS=1 python -m unittest discover tests  # adds two tests that call Claude
python scripts/run_drills.py                         # breaks and restores the live cluster
```

The drills break one deployment at a time (scaled to zero, crash loop, bad image tag), ask Kira what's wrong, grade the answer and put everything back. Results are in [`drills/RESULTS.md`](drills/RESULTS.md). The first runs showed Kira blaming the wrong change, which led to fixes in its tools; the full story is in [`TROUBLESHOOTING.md`](../../TROUBLESHOOTING.md).

## Common problems

- **"model is not available for this account"**: pick a model your account can use and set it as `BEDROCK_MODEL_ID`. Test one with `aws bedrock-runtime converse --model-id <id> --messages '[{"role":"user","content":[{"text":"hi"}]}]'`.
- **Metrics or health tools can't reach Prometheus**: locally, start the port-forward above and set `PROMETHEUS_URL`.
- **`fetch_logs` finds nothing**: the `/eks/boutique/pods` log group appears once Fluent Bit is running (`kubectl get pods -n amazon-cloudwatch`). For demo data, run `python3 scripts/generate_sample_data.py`.
- **Access denied on EKS calls**: Kira's role needs the EKS read permissions from step 1.

## Files

```
app.py            chat UI and approval card
agent.py          agent loop and system prompt
guardrails.py     untrusted output, budgets, rate limit, allowlist
remediation.py    scale / restart / rollback
changes.py        fetch_recent_changes
lambda/           tool code (fetch_logs runs as a Lambda, the others in the pod)
schemas/          tool definitions
scripts/          sample data, incident drills
tests/            unit tests
```
