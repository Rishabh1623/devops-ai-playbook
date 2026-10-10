# AIOps Assistant — Kira

An AI-powered SRE assistant built on Claude (Amazon Bedrock). Kira diagnoses production incidents by querying CloudWatch Logs, Prometheus metrics, and EKS cluster health — then responds with root cause, evidence, and fix recommendations.

---

## Architecture

```
Streamlit UI (app.py)
      │
      ▼
Kira agent loop (agent.py) ──► Claude on Bedrock (Converse API, tool use)
      │  runs each tool Claude asks for
      ├── fetch_logs           → Lambda → CloudWatch Logs
      ├── fetch_metrics        → in-process → Prometheus (ClusterIP, private)
      ├── fetch_service_health → in-process → EKS API + Prometheus (ClusterIP, private)
      ├── fetch_recent_changes → in-process → K8s events/rollouts, Argo CD history, GitHub commits
      └── scale / restart / rollback_deployment
                               → Approve / Reject in the UI → Kubernetes API (boutique only)
```

> **Why do two tools run in-process?** Prometheus has no authentication, so it is only reachable inside the cluster (ClusterIP). Kira itself runs in EKS, so `agent.py` loads `lambda/fetch_metrics` and `lambda/fetch_health` directly and calls them at `http://kube-prometheus-stack-prometheus.monitoring.svc:9090`. Nothing about Prometheus is exposed to the internet.

> **Why not a Bedrock Agent?** Bedrock Agents (classic) is in maintenance mode and new agent creation is blocked on accounts without prior usage. Kira now runs its own agent loop in `agent.py`: it sends the question and the 3 tool definitions (built from `schemas/*.json`) to Claude, runs the matching tool whenever Claude requests one, and feeds the result back until Claude answers. The tools receive the same event format a Bedrock Agent would send, so their code is unchanged.

---

## Prerequisites

- AWS account with access to a Claude model on Bedrock (default: `us.anthropic.claude-sonnet-4-6`)
- EKS cluster running with `kube-prometheus-stack` (Prometheus stays ClusterIP)
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
| `aiops-lambda-role` | The `aiops-fetch-logs` Lambda | CloudWatch Logs read, EKS describe, Lambda basic execution |

The AWS identity that runs `app.py` separately needs `bedrock:InvokeModel` on the Claude inference profile, `lambda:InvokeFunction` on `aiops-fetch-logs`, and `eks:DescribeCluster`, `eks:ListNodegroups`, `eks:DescribeNodegroup` (for `fetch_service_health`). On EKS this is the Terraform-managed Pod Identity role `eks-cluster-aiops-assistant`.

---

## Step 2: Create the fetch_logs Lambda

Create the `aiops-fetch-logs` Lambda in the AWS Console (or via CLI) with the code in `lambda/fetch_logs/lambda_function.py` and the `aiops-lambda-role` execution role.

Runtime: **Python 3.12** | Timeout: **30 seconds**

`fetch_metrics` and `fetch_health` are not Lambdas: they ship inside the Kira image and run in its pod. If you created `aiops-fetch-metrics` and `aiops-fetch-health` before, you can delete them.

---

## Step 3: Prometheus URL

The in-process tools read Prometheus's address from `PROMETHEUS_URL`. The default, `http://kube-prometheus-stack-prometheus.monitoring.svc:9090`, works inside the cluster and is also set in `gitops/k8s/aiops-assistant/deployment.yml`.

When running Kira on your machine, port-forward Prometheus and point Kira at it:

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
export PROMETHEUS_URL=http://localhost:9090
```

Don't expose Prometheus with a LoadBalancer: it has no authentication.

---

## Step 4: Configure the Lambdas

Run the deploy script. It verifies the `aiops-fetch-logs` Lambda exists and sets its timeout to 30s:

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

## What changed? (`fetch_recent_changes`)

Most incidents follow a change, so Kira checks this early in every investigation. For a time window (`hours_back`, default 6, max 48), optionally filtered to one `deployment`, it returns:

| Source | What | Notes |
|--------|------|-------|
| Kubernetes events | scaling, restarts, probe failures, back-offs | The API server keeps events about 1 hour |
| Deployments | replicas, available, revision, and `scaled_by` (who last set replicas, from `managedFields`, e.g. `kubectl` via `scale`) | |
| Rollouts | ReplicaSet revisions created in the window, with image tags | |
| Argo CD | sync status, health, deployed revision, sync history | |
| Commits | recent commits on `project-demo`, with the deployed one marked | GitHub API, no token needed for this public repo (60 requests/hour per IP); set `GITHUB_TOKEN` to raise it. `KIRA_GITHUB_REPO` / `KIRA_GIT_BRANCH` override the defaults |

Each source is fetched separately, so one failing (e.g. a missing permission) returns its error and the others still come back. Results are capped (30 events, 15 rollouts, 10 syncs, 15 commits), about 3–4k tokens per call.

**Permissions:** the `aiops-assistant-read` Role in `gitops/k8s/aiops-assistant/rbac.yml` (`list` events and deployments in `boutique`), and the `aiops-assistant-read-app` Role in `projects/Infrastructure/modules/argocd/main.tf` (`get` on the `boutique` Argo CD Application only). The second is in Terraform because the GitOps kustomization puts every resource in `boutique`.

---

## Incident drills

`scripts/run_drills.py` checks that Kira still finds root causes after a change to the prompt, model (`BEDROCK_MODEL_ID`) or tools. For each scenario it breaks one deployment on purpose, asks Kira the same vague question ("something is wrong with the shop… which service and what caused it?"), scores the answer, and puts the deployment back:

| Scenario | Failure | Outage? | Kira must name | Cause keywords |
|----------|---------|---------|----------------|----------------|
| `scaled_to_zero` | `orders` scaled to 0 | yes, while the drill runs (~1–2 min) | `orders` | scaled to 0 / 0 replicas |
| `crash_loop` | `product-service` pods exit 1 on start | no, the old pod keeps serving | `product-service` | crash / exit 1 / back-off / restart |
| `bad_image` | `user-service` image tag that doesn't exist | no, the old pod keeps serving | `user-service` | image / pull |

Every scenario must also call `fetch_recent_changes`. Scoring is by keywords: cheap and repeatable, but strict about wording, so read the full answers in `drills/results.jsonl` when one fails.

```bash
python scripts/run_drills.py --list
python scripts/run_drills.py                    # all scenarios, asks to confirm
python scripts/run_drills.py -s bad_image --yes
```

- Needs `kubectl` access (`~/.kube/config`) and AWS credentials for Bedrock and the `aiops-fetch-logs` Lambda. Prometheus is reached through a `kubectl port-forward` the script starts, unless `PROMETHEUS_URL` is set.
- Argo CD auto-sync is paused for the run (self-heal would undo the failures) and put back afterwards.
- Each deployment is saved before the failure and restored after, including on errors and Ctrl-C, and the script waits until it is healthy again.
- Kira's proposed fixes are recorded but never run.
- Results are appended to `drills/RESULTS.md` (one table per run) and `drills/results.jsonl` (full answers). The exit code is non-zero if any drill fails or doesn't restore.
- Each run costs Claude tokens: roughly 10–30k per scenario.

---

## Remediation (with approval)

Kira can fix what it finds, but only after an engineer approves each action:

| Tool | What it does |
|------|--------------|
| `scale_deployment` | Set replicas (1–5) |
| `restart_deployment` | Rolling restart (like `kubectl rollout restart`) |
| `rollback_deployment` | Previous revision (like `kubectl rollout undo`) |

1. When Claude asks for one of these, the agent loop pauses (`chat()` returns a `PendingAction`). Nothing has run yet.
2. The UI shows an approval card: the action, Kira's reason, the current state, the change, and any Argo CD warning. Chat input is disabled until the engineer clicks **Approve** or **Reject**.
3. On approval, `remediation.execute()` re-checks the allowlist, makes the change, and waits up to 90s for the rollout. Kira then calls `fetch_service_health` and reports whether the fix worked.

Every step (`proposed`, `blocked`, `approved`, `rejected`, `executed`, `failed`) is logged as one JSON line with a `kira_action` field, which Fluent Bit ships to `/eks/boutique/pods`:

```bash
aws logs filter-log-events --log-group-name /eks/boutique/pods --filter-pattern '"kira_action"'
```

**Permissions:** the `aiops-assistant` service account gets the Role in `gitops/k8s/aiops-assistant/rbac.yml`: `get`/`patch` on the 7 named deployments and their scale, `get` on their status, and `list` on ReplicaSets (for rollback). It can't touch Kira's own deployment, Postgres, or other namespaces. Locally, Kira uses your `~/.kube/config`.

**GitOps rule:** Kira's changes are temporary fixes; the fix becomes permanent when someone commits it to git.
- Scale and restart: `gitops/argo-cd.yml` tells Argo CD to ignore `/spec/replicas` and the `restartedAt` annotation, so self-heal doesn't undo them. The next sync from git (any push to `project-demo`) resets replicas to the value in git.
- Rollback: changes the pod template, which Argo CD does track. With self-heal on, Argo CD reverts it to the image in git within seconds, so revert the bad commit in git instead. The approval card warns about this.

---

## Guardrails

Kira's safety and cost limits live in `guardrails.py`:

| Guardrail | What it does | Setting (default) |
|-----------|--------------|-------------------|
| Untrusted tool output | Every tool result is wrapped as `{"untrusted_tool_output": ...}`, and the system prompt tells Claude never to follow instructions inside it. Log lines can be written by any pod, so this defends against prompt injection. | — |
| Token budget | Input + output tokens are counted from each Converse `usage`. Once a question reaches the budget, Kira stops with a message. | `KIRA_MAX_TOKENS_PER_QUESTION` (`100000`) |
| Rate limit | Questions per browser session in a sliding window. "New Session" does not reset it. | `KIRA_MAX_QUESTIONS_PER_WINDOW` (`10`) per `KIRA_RATE_WINDOW_SECONDS` (`300`) |
| Action allowlist | `check_action()` allows only scale / restart / roll back of the 7 boutique app deployments (not Kira or Postgres), with 1–5 replicas. Checked when an action is proposed and again before it runs. | edit `guardrails.py` |

The prompt instruction reduces prompt-injection risk but cannot remove it, which is why write actions are checked in code.

Run the tests (the live prompt-injection test calls Claude on Bedrock and is opt-in):

```bash
python -m unittest discover tests
KIRA_LIVE_TESTS=1 python -m unittest discover tests
```

---

## Project Structure

```
aiops-assistant/
├── app.py                  # Streamlit chat UI
├── agent.py                # Kira agent loop (Claude + tools)
├── guardrails.py           # Untrusted-output wrapping, budgets, rate limit, allowlist
├── remediation.py          # Write tools: scale / restart / rollback (with approval)
├── changes.py              # fetch_recent_changes: events, rollouts, Argo CD syncs, commits
├── k8s.py                  # Kubernetes client setup shared by the in-process tools
├── deploy.sh               # fetch_logs Lambda checks / configuration
├── setup-iam.sh            # Lambda IAM role and policies setup
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── lambda/
│   ├── fetch_logs/         # CloudWatch Logs query (Lambda)
│   ├── fetch_metrics/      # Prometheus metrics query (runs in Kira)
│   └── fetch_health/       # EKS cluster health check (runs in Kira)
├── schemas/
│   ├── fetch_logs.json     # Tool definition for fetch_logs (OpenAPI)
│   ├── fetch_metrics.json  # Tool definition for fetch_metrics (OpenAPI)
│   └── fetch_health.json   # Tool definition for fetch_health (OpenAPI)
├── scripts/
│   ├── generate_sample_data.py  # Seed CloudWatch with test errors
│   └── run_drills.py       # Incident drills: break, ask Kira, score, restore
├── drills/                 # Drill results (RESULTS.md, results.jsonl)
└── tests/
    ├── test_guardrails.py  # Guardrail and prompt-injection tests
    ├── test_remediation.py # Approval flow and write tool tests
    ├── test_changes.py     # fetch_recent_changes tests (+ opt-in live demo)
    └── test_drills.py      # Drill script tests (no cluster needed)
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

### fetch_metrics / fetch_health can't reach Prometheus
These tools run in the Kira process and call `PROMETHEUS_URL`. In the cluster, check the service exists: `kubectl get svc kube-prometheus-stack-prometheus -n monitoring`. When running Kira locally, the in-cluster address doesn't resolve: start the `kubectl port-forward` from Step 3 and set `PROMETHEUS_URL=http://localhost:9090`.

### fetch_logs returns no results
The default log group is `/eks/boutique/pods`. This group is only created after Fluent Bit starts shipping logs. Make sure `aws-for-fluent-bit` is running:

```bash
kubectl get pods -n amazon-cloudwatch
```

If the log group doesn't exist yet, run the sample data generator first (Step 5) which creates `/app/production`.

### fetch_health uses wrong cluster name
The tool defaults to cluster name `eks-cluster`. If your cluster has a different name, update `DEFAULT_CLUSTER` in `lambda/fetch_health/lambda_function.py` and rebuild the Kira image.

### fetch_health access denied on eks:DescribeCluster
`fetch_health` runs with Kira's own credentials. On EKS, check the Pod Identity role has the `ReadClusterHealth` statements (`terraform apply` in `projects/Infrastructure`):

```bash
aws iam get-role-policy \
  --role-name eks-cluster-aiops-assistant \
  --policy-name bedrock-and-tools
```

Locally, your CLI identity needs the same EKS read permissions.

### AWS credentials not resolving in Streamlit
If `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are left blank in `.env`, boto3 falls back to the default credential chain (`~/.aws/credentials`, environment variables, IAM role). If none of those are configured, Bedrock calls will fail with an auth error. Either fill in the credentials in `.env` or ensure your terminal session has valid AWS credentials before starting Streamlit.
