# DevOps + AIOps on AWS EKS: a learning project

I built this while working through the **DevOps + AIOps Series** by [Vishakha Sadhwani](https://github.com/vishakhasadhwani) ([original repo](https://github.com/vishakhasadhwani/devops-ai-playbook)). I followed the series, deployed everything to my own AWS account, fixed what broke along the way, and then extended the AI part of the project.

## What the project is

A small online shop (React frontend, an API gateway, Node.js services and PostgreSQL) running on Kubernetes on AWS EKS, with a full delivery setup around it:

- Terraform creates the AWS infrastructure (VPC, EKS, ECR, IAM)
- GitHub Actions builds the images and pushes them to ECR
- Argo CD deploys whatever is in the `gitops/` folder (GitOps)
- Prometheus and Grafana for metrics, Fluent Bit ships logs to CloudWatch

## The problem the AI part solves

When something breaks in a system like this, the first part of an incident is spent digging through logs, metrics and recent deployments to work out what went wrong. **Kira** is an assistant that does that first investigation. You describe the symptom, and Kira pulls the logs, metrics, pod health and recent changes, then answers with the likely root cause and the evidence for it. If the fix is simple (scale up, restart, roll back), Kira can propose it, and nothing runs until a person clicks Approve.

```
git push → GitHub Actions → ECR → Argo CD → EKS (the shop)

engineer → Kira → Claude on AWS Bedrock
             ├─ logs            CloudWatch
             ├─ metrics         Prometheus
             ├─ pod health      EKS / Kubernetes
             ├─ recent changes  events, Argo CD syncs, git commits
             └─ fixes           only after approval
```

## What I added on top of the series

- Rebuilt Kira as its own agent loop with Claude on Bedrock, because Bedrock Agents can no longer be created on new AWS accounts
- Deployed Kira to EKS through the same CI and GitOps pipeline as the shop
- Guardrails: tool output is treated as untrusted (a log line can contain prompt-injection text), with a token budget per question, a rate limit, and an allowlist of actions checked in code
- Fixes with approval: Kira can scale, restart or roll back a deployment only after a person approves, using narrow Kubernetes permissions, and every action is logged
- A "what changed?" tool, so Kira can connect a symptom to the change that caused it
- Incident drills: a script that breaks the cluster on purpose (scale to zero, crash loop, bad image), asks Kira, grades the answer and puts everything back. Results are in [`drills/RESULTS.md`](projects/aiops-assistant/drills/RESULTS.md)
- Security and platform fixes: CI signs in to AWS with GitHub OIDC instead of stored keys, the database password lives in Secrets Manager, Terraform state is in S3 with locking, Prometheus is private again, and every service has health checks and resource limits
- Python tests for Kira: `cd projects/aiops-assistant && python -m unittest discover tests`

## Problems I ran into

The ones that taught me the most are written up in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md):

- **A practice incident:** the orders service scaled to zero, and how Kira and I traced it
- **Prometheus open to the internet:** it had no login and was public only so Kira's tools, running outside the cluster, could reach it. I moved those tools into the cluster instead
- **Kira blaming the wrong change:** my first drills passed, but reading the answers showed Kira blaming an unrelated deployment. A stricter grader caught it, and the real causes were missing data in Kira's tool and the fact that `kubectl scale` doesn't record who ran it

Notes on earlier bugs from the series are in [`projects/Issues.md`](projects/Issues.md).

## Run it yourself

- Deployment guide (from the series): [`projects/README.md`](projects/README.md)
- Kira: [`projects/aiops-assistant/README.md`](projects/aiops-assistant/README.md)

## Credits

The series, the boutique application, the original Terraform, GitOps and CI setup, and everything in `docs/` are the work of **Vishakha Sadhwani**, with contributions from **Anish Bhat K**.

Everything below this line is the original series README, unchanged. One difference in this fork: Kira uses Claude through the Bedrock Converse API instead of a Bedrock Agent.

---

## Welcome

Hey everyone!

Welcome to my DevOps + AI series where we build an end-to-end DevOps project with an AIOps integration.

A lot of you have been asking: *"when are you going to share a full DevOps project?"*

Well — here we are.

In this series we will:

- Build microservices locally
- Use Claude and AI tools to assist development
- Deploy everything step by step
- Migrate the system to the cloud on AWS EKS
- Set up a full CI/CD pipeline with GitHub Actions
- Implement GitOps workflows with ArgoCD
- Integrate AIOps capabilities with AWS Bedrock

By the end of this series, you won't just know tools — you'll understand how real DevOps systems are designed and deployed.

---

## Repository Structure

```
DevOps-Practice-Guide/
├── docs/
│   ├── part1-system-design.md     # System design foundations (Part 1)
│   ├── part2-workflow.md          # Full workflow with AIOps (Part 2)
│   └── claude-setup.md            # Claude Code + MCP server setup
├── projects/
│   ├── README.md                  # EKS deployment guide (Part 3)
│   ├── boutique-microservices/    # The application (7 services)
│   ├── Infrastructure/            # Terraform for AWS provisioning
│   └── aiops-assistant/           # Bedrock Agent — Kira (Part 4)
├── gitops/
│   ├── argo-cd.yml                # ArgoCD Application manifest
│   ├── kustomization.yml          # Kustomize entry point
│   └── k8s/                       # All Kubernetes manifests
└── .github/
    └── workflows/ci.yml           # GitHub Actions CI pipeline
```

---

## Series Structure

### Claude Setup — AI Assistant Configuration
[`docs/claude-setup.md`](docs/claude-setup.md)

Before jumping into the project, this step walks through how Claude Code is configured as the AI assistant throughout this series.

Three things are set up:

**CLAUDE.md** — a project instruction file at the repo root that Claude reads automatically at the start of every session. It puts Claude in safe execution mode: explain what you're about to do and why before taking any action. This is important when working with live AWS infrastructure where silent commands can have real consequences.

**MCP Servers** — background processes that extend Claude's built-in capabilities. Four servers are configured in `~/.claude/settings.json`:

| Server | What it unlocks |
|--------|----------------|
| `awslabs.eks-mcp-server` | Query EKS clusters, inspect pods, stream logs, apply manifests |
| `awslabs.terraform-mcp-server` | Run Terraform commands, search provider docs, run Checkov scans |
| `awslabs.aws-pricing-mcp-server` | Live AWS pricing lookups and cost analysis reports |
| `awslabs.core-mcp-server` | MCP orchestration layer (deprecated, kept for compatibility) |

**Skills** — domain-specific knowledge packs that improve how Claude reasons about certain topics. The `terraform-skill` is installed, giving Claude deeper context for Terraform module patterns, testing strategies, security scanning, and CI/CD workflows specific to infrastructure-as-code.

---

### Part 1 — System Design Foundations
[`docs/part1-system-design.md`](docs/part1-system-design.md)

We start with system design concepts specifically for cloud and DevOps. This is important whether you're a beginner, intermediate, or senior engineer — because companies don't choose tools randomly. They think about architecture patterns, deployment strategies, scalability, reliability, and cost tradeoffs.

We cover 12 core system design pillars used in modern DevOps architectures, and connect each one directly to something running in this project.

---

### Part 2 — Understanding the Workflow
[`docs/part2-workflow.md`](docs/part2-workflow.md)

Before writing any code or deployment configs, you need to understand how the entire system flows:

- What services we're building and how they communicate
- How the pipeline works
- How code moves from developer → CI → deployment → production → AIOps

This is where the full picture comes together — including how AI fits into the workflow.

---

### Part 3 — DevOps Project Implementation
[`projects/README.md`](projects/README.md)

Then we actually build the project. You'll see:

- Docker containers and Docker Compose
- Kubernetes deployments on EKS
- CI/CD pipelines with GitHub Actions
- GitOps automation with ArgoCD
- Infrastructure provisioning with Terraform
- Observability with Prometheus and Grafana

---

### Part 4 — AIOps Integration
[`projects/aiops-assistant/README.md`](projects/aiops-assistant/README.md)

Finally, we explore how AI helps with:

- Monitoring and anomaly detection
- Log analysis at scale
- Incident response automation
- DevOps troubleshooting

Because modern DevOps is no longer just automation — it's **automation + intelligence**.

---

## Bonus Challenge

You'll get access to this entire repository.

But there's a catch.

The repository includes **intentional issues and troubleshooting tasks**.

Why? Because AI has made things easier. But if you want to grow as an engineer, you must learn how to break systems, debug systems, and fix systems.

Once you implement the project:

1. Fork the repository
2. Deploy the system
3. Troubleshoot the issues
4. Share what you learned — and tag me so I know you're building along

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Application | React, Node.js, PostgreSQL |
| Containers | Docker, Docker Compose |
| Orchestration | Kubernetes (AWS EKS) |
| Infrastructure | Terraform |
| CI/CD | GitHub Actions |
| GitOps | ArgoCD + Kustomize |
| Monitoring | Prometheus + Grafana |
| Log Forwarding | AWS Fluent Bit → CloudWatch |
| AIOps | AWS Bedrock Agent (Kira) |
| AI Assistant | Claude Code + MCP Servers |
