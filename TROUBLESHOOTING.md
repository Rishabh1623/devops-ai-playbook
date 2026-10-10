# Troubleshooting

Complete incident walk-throughs from running this project, in the form **symptom → investigation → root cause → fix → prevention**. Shorter problem/solution notes from the original build (node pod capacity, EBS permissions, the Postgres init script, missing app metrics) are in [`projects/Issues.md`](projects/Issues.md).

1. [Orders can't be placed: `orders` scaled to 0](#1-orders-cant-be-placed-orders-scaled-to-0) (incident drill, investigated with Kira)
2. [Prometheus readable from the internet](#2-prometheus-readable-from-the-internet) (security)
3. [Kira named the right service but blamed the wrong change](#3-kira-named-the-right-service-but-blamed-the-wrong-change) (debugging the AI assistant)

---

## 1. Orders can't be placed: `orders` scaled to 0

This is the `scaled_to_zero` incident drill (`projects/aiops-assistant/scripts/run_drills.py`). It ran against the live cluster on 2026-10-10; the results are in [`drills/RESULTS.md`](projects/aiops-assistant/drills/RESULTS.md).

**Symptom.** Customers can't place orders. The gateway returns `503` for order requests. Nothing has been deployed on purpose.

**Investigation.** Asked Kira: *"Something is wrong with the shop in the boutique namespace. Investigate and tell me the root cause: which service is affected and what caused it."* In 39 seconds and about 26k tokens, Kira called:

| Tool | What Kira used from it |
|------|------------------------|
| `fetch_service_health` | `orders` has 0 desired replicas and no pods |
| `fetch_recent_changes` | Event `Scaled down replica set orders-5d69ccc465 from 1 to 0` at 02:20:21Z; `orders` has `replicas_set_via_scale: true`; `changed_by` shows Argo CD's last sync changed only the image, not replicas |
| `fetch_logs`, `fetch_metrics` | Also called; the `orders` diagnosis rests on the two tools above |

The same checks by hand:

```bash
kubectl get deploy orders -n boutique                          # READY 0/0
kubectl get events -n boutique --field-selector reason=ScalingReplicaSet
kubectl get deploy orders -n boutique -o yaml --show-managed-fields | grep -A3 manager
```

**Root cause.** Kira's answer: *"`orders` — Manually scaled to zero by an unknown actor outside git… `replicas_set_via_scale: true`… Kubernetes event: `Scaled down replica set orders-5d69ccc465 from 1 to 0` at 2026-10-10T02:20:21Z."* That is correct: the drill ran the equivalent of `kubectl scale deployment orders --replicas=0`. Kubernetes doesn't record *who* scales through the scale endpoint, so "unknown actor, at this time" is the most the cluster can tell you.

Not everything in the answer was right. This was the fourth drill run in a row, and Kira also reported `product-service` as still crash-looping from logs and events left by the previous run, although it had already been restored. Recent events and logs outlive the problem that caused them: check the current state (`kubectl get pods`) before acting on them.

**Fix.** Scale back to the replica count in git:

```bash
kubectl scale deployment orders -n boutique --replicas=1
```

Kira recommended the same ("Scale back to 1"). Asked to fix it, Kira proposes `scale_deployment orders → 1` on the approval card and re-checks health after you approve. (In drills the script restores the deployment itself and never runs Kira's proposals.)

**Prevention.**
- Turn on Argo CD self-heal (`gitops/argo-cd.yml`) so manual drift is noticed. Note that it ignores `/spec/replicas` on purpose, so Kira's temporary scale fixes aren't undone; a manual scale to 0 stays until the next sync from git.
- To know *who* scaled, enable EKS control plane audit logs (CloudWatch). `managedFields` can't tell you.
- Run the drills after changing Kira's prompt, model or tools: `python scripts/run_drills.py`.

---

## 2. Prometheus readable from the internet

**Symptom.** `kubectl get svc -n monitoring` showed `kube-prometheus-stack-prometheus` as `type: LoadBalancer` with a public ELB hostname on port 9090. Prometheus has no authentication: anyone with the URL could read every cluster metric.

**Investigation.** It was made public on purpose (`ed9f816`) so Kira's `fetch_metrics` and `fetch_health` Lambdas, which run outside the VPC, could reach it. The options were:

| Option | Trade-off |
|--------|-----------|
| Amazon Managed Prometheus, Lambdas query it with SigV4 | Pay per sample; needs remote-write |
| Lambdas inside the VPC | Needs NAT or VPC endpoints for the AWS APIs, plus an internal load balancer |
| Run those two tools in the Kira pod, which is already in the cluster | No new services or cost |

**Root cause.** Tools that needed in-cluster data ran outside the cluster.

**Fix.** The third option (#6): `agent.py` runs `fetch_metrics` and `fetch_service_health` in-process and queries `http://kube-prometheus-stack-prometheus.monitoring.svc:9090`; Prometheus is back to `ClusterIP` (`projects/Infrastructure/modules/argocd/main.tf`). Applying it:

```bash
cd projects/Infrastructure && terraform apply
kubectl get svc kube-prometheus-stack-prometheus -n monitoring   # TYPE ClusterIP, no EXTERNAL-IP
```

A follow-up lesson: the code change was merged but `terraform apply` hadn't been run, so the public endpoint stayed up. `terraform plan` in a later issue (#3) showed the two pending changes. Check with `plan` after merging infrastructure changes.

**Prevention.** Don't expose unauthenticated services through `type: LoadBalancer`. To look at Prometheus from a laptop, port-forward instead:

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
```

---

## 3. Kira named the right service but blamed the wrong change

**Symptom.** The first live drill run scored 3/3 with keyword checks, but reading the answers showed Kira never identified the real trigger: it blamed an unrelated CI commit or Argo CD sync in two of three, and in the third named only the raw client name in `managedFields` (`OpenAPI-Generator`). For example: *"The orders deployment was scaled to 0 by Argo CD after git commit 807ccc8 set replicas: 0 in the manifest."* Commit `807ccc8` was a CI commit that only changed the 8 image lines (`git show --stat 807ccc8`).

**Investigation.**
1. Added an LLM grader to the drills that compares each answer with what really happened. Re-grading the first run's answers: 0/3 correct triggers.
2. Looked at what Kira's `fetch_recent_changes` tool actually returned. It showed who changed *replicas* but not the pod template, and rollouts showed images but not commands, so a command override looked like "same image, new revision".
3. Checked `managedFields` after a real `kubectl scale` in a throwaway namespace: **the scale endpoint drops the old owner and records no new one**. The tool's assumption that `kubectl scale` would show up there was wrong.

**Root cause.** Kira didn't have the evidence to attribute changes. With a CI deploy happening at the same time, it linked the symptom to the nearest change it could see.

**Fix** (#9):
- `fetch_recent_changes` reports `changed_by` (manager and fields: image, command, …), rollout `commands`, and `replicas_set_via_scale`.
- Kira's prompt: blame a change only when the evidence ties it to the broken object; CI "update image tags" commits never change replicas or commands; say "unknown" rather than guess.
- The drills inject failures as field manager `kubectl`, like an engineer would.

| Run | Result |
|-----|--------|
| 1 (keywords only) | 3/3 on keywords, 0/3 correct triggers |
| 2 | 1/3 |
| 3 | 2/3 |
| 4 | 3/3 |

**Prevention.**
- Score AI answers against ground truth, not just keywords: a right answer and a wrong answer can use the same words.
- Re-run the drills after any change to Kira (`python scripts/run_drills.py`), and read `drills/results.jsonl` when one fails.
- Known limitations: the crash-loop drill's error text says `(drill)`, which hints to Kira that it's a test; one run of each scenario can vary, so repeat before trusting a single result.
