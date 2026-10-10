#!/usr/bin/env python3
"""
Kira incident drills (#9): break something on purpose, ask Kira, score the
answer, put the cluster back.

For each scenario the script
  1. saves the target deployment, then injects a known failure
  2. waits until the failure is visible
  3. asks Kira the same vague question ("something is wrong with the shop")
  4. scores the answer: keyword checks (right service, cause, tools) and an
     LLM grader that compares it with what the drill really did, so an
     answer naming the right service but blaming the wrong change fails
  5. restores the deployment and waits until it is healthy again (always,
     including on errors and Ctrl-C)

Argo CD auto-sync is paused during the run, otherwise self-heal would undo the
injected failures, and restored afterwards. Kira's proposed fixes are recorded
but never run: the script restores the cluster itself.

Runs from a machine with kubectl access (~/.kube/config) and AWS credentials
for Bedrock and the fetch_logs Lambda. Prometheus is reached with a
kubectl port-forward unless PROMETHEUS_URL is set.

    python scripts/run_drills.py --list
    python scripts/run_drills.py                 # all scenarios, asks to confirm
    python scripts/run_drills.py -s crash_loop --yes

Only scaled_to_zero causes an outage (orders, ~1-2 minutes). The others roll out
a broken pod while the old one keeps serving.

Results are appended to drills/RESULTS.md (table) and drills/results.jsonl
(full answers).
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

NAMESPACE = "boutique"
ARGOCD_NAMESPACE, ARGOCD_APP = "argocd", "boutique"
DRILLS_DIR = APP_DIR / "drills"
QUESTION = (
    "Something is wrong with the shop in the boutique namespace. Investigate and tell me "
    "the root cause: which service is affected and what caused it."
)
FAILURE_TIMEOUT_SECONDS = 180
POLL_SECONDS = 3
# Failures are injected as an engineer would make them, so Kira sees the same
# managedFields owner a real `kubectl scale` / `kubectl set image` leaves.
INJECT_MANAGER = "kubectl"
RESTORE_MANAGER = "kira-drill-restore"


@dataclass
class Scenario:
    name: str
    description: str
    deployment: str
    cause: str            # regex the answer must match (case-insensitive)
    tools: tuple          # tools Kira must call
    truth: str = ""       # what really happened, for the LLM grader
    outage: bool = False

    def inject(self, apps, dep):
        raise NotImplementedError

    def failure_visible(self, apps, core):
        raise NotImplementedError


class ScaledToZero(Scenario):
    def inject(self, apps, dep):
        apps.patch_namespaced_deployment_scale(self.deployment, NAMESPACE, {"spec": {"replicas": 0}},
                                               field_manager=INJECT_MANAGER)

    def failure_visible(self, apps, core):
        d = apps.read_namespaced_deployment(self.deployment, NAMESPACE)
        return d.spec.replicas == 0 and not d.status.available_replicas


class _BadPod(Scenario):
    waiting_reasons = ()

    def failure_visible(self, apps, core):
        for pod in _pods(apps, core, self.deployment):
            for cs in pod.status.container_statuses or []:
                waiting = cs.state.waiting.reason if cs.state and cs.state.waiting else None
                if waiting in self.waiting_reasons:
                    return True
        return False


class CrashLoop(_BadPod):
    waiting_reasons = ("CrashLoopBackOff",)

    def inject(self, apps, dep):
        container = dep.spec.template.spec.containers[0].name
        apps.patch_namespaced_deployment(self.deployment, NAMESPACE, {"spec": {"template": {"spec": {"containers": [{
            "name": container,
            "command": ["node", "-e", "console.error('FATAL: cannot connect to config store (drill)'); process.exit(1)"],
        }]}}}}, field_manager=INJECT_MANAGER)


class BadImage(_BadPod):
    waiting_reasons = ("ErrImagePull", "ImagePullBackOff")

    def inject(self, apps, dep):
        container = dep.spec.template.spec.containers[0]
        repo = container.image.rsplit(":", 1)[0]
        apps.patch_namespaced_deployment(self.deployment, NAMESPACE, {"spec": {"template": {"spec": {"containers": [{
            "name": container.name, "image": f"{repo}:drill-tag-does-not-exist",
        }]}}}}, field_manager=INJECT_MANAGER)


SCENARIOS = {s.name: s for s in [
    ScaledToZero(
        name="scaled_to_zero",
        description="orders scaled to 0 replicas (outage while the drill runs)",
        deployment="orders",
        cause=r"scal\w*\s+(down\s+)?to\s+(0|zero)|\b(0|zero)\s+replicas|replicas\W{0,5}0\b",
        tools=("fetch_recent_changes",),
        truth=("An engineer ran `kubectl scale deployment orders --replicas=0`, a manual scale outside git. "
               "Kubernetes doesn't record who scales through the scale endpoint, so the best possible answer "
               "is a manual scale by an unknown actor (naming kubectl is also fine). Nothing in git or Argo CD "
               "changed orders' replicas; any commits or syncs around that time are unrelated."),
        outage=True,
    ),
    CrashLoop(
        name="crash_loop",
        description="product-service new pods crash on start (old pod keeps serving)",
        deployment="product-service",
        cause=r"crash|exit(s|ed)?\s+(with\s+)?(code\s+)?1|back-?off|restart",
        tools=("fetch_recent_changes",),
        truth=("An engineer used kubectl to override product-service's container command so the process "
               "prints 'FATAL: cannot connect to config store (drill)' and exits 1. The image did not change. "
               "Any commits or syncs around that time are unrelated."),
    ),
    BadImage(
        name="bad_image",
        description="user-service rolled out with an image tag that doesn't exist (old pod keeps serving)",
        deployment="user-service",
        cause=r"image|pull",
        tools=("fetch_recent_changes",),
        truth=("An engineer used kubectl to set user-service's image tag to 'drill-tag-does-not-exist', "
               "which isn't in ECR, so new pods can't pull it. No git commit changed it; any commits or "
               "syncs around that time are unrelated."),
    ),
]}


def score(scenario, answer, tools_called):
    """Keyword scoring: right service, right cause, right tools."""
    text = answer or ""
    result = {
        "service": re.search(rf"(?<![\w-]){re.escape(scenario.deployment)}(?![\w-])", text, re.I) is not None,
        "cause": re.search(scenario.cause, text, re.I) is not None,
        "tools": set(scenario.tools) <= set(tools_called),
    }
    result["passed"] = all(result.values())
    return result


GRADER_PROMPT = """You grade an SRE assistant's incident diagnosis against what really happened.

What really happened:
{truth}

Affected deployment: {deployment}

Earlier drills in the same run may have broken and restored other services a few minutes before, so
recent events for them can appear. Mentioning those is not an error if {deployment} is diagnosed correctly.

The assistant's answer:
<answer>
{answer}
</answer>

Return only a JSON object:
{{"service": true/false,  // named {deployment} as the affected service
  "cause": true/false,    // described the failure correctly
  "trigger": true/false,  // named the real trigger, or said the trigger is unknown; false if it blamed an unrelated change (e.g. a commit or sync that didn't cause it)
  "notes": "one sentence explaining any false"}}"""


class LLMGrader:
    """Claude on Bedrock compares Kira's answer with the scenario's ground truth."""

    def __init__(self, bedrock, model_id):
        self.bedrock, self.model_id = bedrock, model_id

    def __call__(self, scenario, answer):
        resp = self.bedrock.converse(
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": GRADER_PROMPT.format(
                truth=scenario.truth, deployment=scenario.deployment, answer=answer or "(no answer)")}]}],
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        match = re.search(r"\{.*\}", text, re.S)
        verdict = json.loads(match.group(0)) if match else {}
        grade = {k: bool(verdict.get(k)) for k in ("service", "cause", "trigger")}
        grade["passed"] = all(grade.values())
        grade["notes"] = verdict.get("notes", "" if match else f"unparseable grader output: {text[:200]}")
        grade["tokens"] = resp.get("usage", {}).get("totalTokens", 0)
        return grade


# --- Kubernetes helpers ---

def _pods(apps, core, deployment):
    d = apps.read_namespaced_deployment(deployment, NAMESPACE)
    selector = ",".join(f"{k}={v}" for k, v in d.spec.selector.match_labels.items())
    return core.list_namespaced_pod(NAMESPACE, label_selector=selector).items


def wait_for(check, timeout, what):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(POLL_SECONDS)


def snapshot(apps, deployment):
    """What restore() needs: replicas and the pod template."""
    d = apps.read_namespaced_deployment(deployment, NAMESPACE)
    return {"replicas": d.spec.replicas, "template": apps.api_client.sanitize_for_serialization(d.spec.template)}, d


def restore(apps, deployment, saved):
    apps.patch_namespaced_deployment(deployment, NAMESPACE, [
        {"op": "replace", "path": "/spec/template", "value": saved["template"]},
        {"op": "replace", "path": "/spec/replicas", "value": saved["replicas"]},
    ], field_manager=RESTORE_MANAGER)


def healthy(apps, deployment):
    d = apps.read_namespaced_deployment(deployment, NAMESPACE)
    s, want = d.status, d.spec.replicas or 0
    return ((s.observed_generation or 0) >= d.metadata.generation and (s.updated_replicas or 0) == want
            and (s.available_replicas or 0) == want and (s.replicas or 0) == want)


class ArgoPause:
    """Turn off Argo CD auto-sync for the run and put the original policy back."""

    def __init__(self, custom):
        self.custom = custom
        self.original = None

    def _app(self):
        return self.custom.get_namespaced_custom_object("argoproj.io", "v1alpha1", ARGOCD_NAMESPACE, "applications", ARGOCD_APP)

    def __enter__(self):
        self.original = self._app()["spec"].get("syncPolicy", {})
        if "automated" in self.original:
            self.custom.patch_namespaced_custom_object(
                "argoproj.io", "v1alpha1", ARGOCD_NAMESPACE, "applications", ARGOCD_APP,
                {"spec": {"syncPolicy": {"automated": None}}})
            print("⏸  Argo CD auto-sync paused")
        return self

    def __exit__(self, *exc):
        if "automated" in (self.original or {}):
            self.custom.patch_namespaced_custom_object(
                "argoproj.io", "v1alpha1", ARGOCD_NAMESPACE, "applications", ARGOCD_APP,
                {"spec": {"syncPolicy": {"automated": self.original["automated"]}}})
            print("▶  Argo CD auto-sync restored")
        return False


# --- Kira ---

def ask_kira(kira):
    from guardrails import TokenBudget

    tools, budget = [], TokenBudget()
    history = [{"role": "user", "content": [{"text": QUESTION}]}]
    result = kira.chat(history, on_tool_call=lambda name, _: tools.append(name), budget=budget, session_id="drill")
    if result.pending:
        # Never act during a drill: record the proposed fix as part of the answer.
        p = result.pending
        answer = f"[proposed {p.name} {json.dumps({k: v for k, v in p.input.items() if k != 'reason'})}] {p.input['reason']}"
        proposed = {"action": p.name, **p.input}
    else:
        answer, proposed = result.text, None
    return {"answer": answer, "tools_called": tools, "proposed_fix": proposed, "tokens": budget.used}


def run_scenario(scenario, apps, core, kira, log=print, grader=None):
    started = time.monotonic()
    saved, dep = snapshot(apps, scenario.deployment)
    outcome = {"scenario": scenario.name, "deployment": scenario.deployment}
    try:
        log(f"💥 {scenario.name}: {scenario.description}")
        scenario.inject(apps, dep)
        wait_for(lambda: scenario.failure_visible(apps, core), FAILURE_TIMEOUT_SECONDS, f"{scenario.name} failure")
        log("🔍 failure visible, asking Kira...")
        outcome.update(ask_kira(kira))
        outcome["score"] = score(scenario, outcome["answer"], outcome["tools_called"])
        if grader:
            outcome["grade"] = grader(scenario, outcome["answer"])
            outcome["score"]["passed"] = outcome["score"]["passed"] and outcome["grade"]["passed"]
    except Exception as e:
        outcome.update(error=f"{type(e).__name__}: {e}", score={"service": False, "cause": False, "tools": False, "passed": False})
    finally:
        log(f"🩹 restoring {scenario.deployment}...")
        restore(apps, scenario.deployment, saved)
        try:
            wait_for(lambda: healthy(apps, scenario.deployment), FAILURE_TIMEOUT_SECONDS, f"{scenario.deployment} healthy")
            outcome["restored"] = True
        except TimeoutError as e:
            outcome["restored"] = False
            outcome.setdefault("error", str(e))
        outcome["seconds"] = round(time.monotonic() - started)
    return outcome


# --- Results ---

def record(outcomes, model, drills_dir=DRILLS_DIR, now=None):
    drills_dir.mkdir(exist_ok=True)
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    with open(drills_dir / "results.jsonl", "a") as f:
        for o in outcomes:
            f.write(json.dumps({"time": when, "model": model, **o}) + "\n")

    results = drills_dir / "RESULTS.md"
    if not results.exists():
        results.write_text(
            "# Kira drill results\n\nWritten by `scripts/run_drills.py`; full answers in `results.jsonl`.\n"
            "✅/❌: named the right service · named the right cause · called the expected tools.\n")
    tick = lambda ok: "✅" if ok else "❌"
    lines = [f"\n## {when} · `{model}`\n",
             "| Scenario | Service | Cause | Tools | Grader | Result | Restored | Tools called | Tokens | Time |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for o in outcomes:
        s = o["score"]
        result = "**pass**" if s["passed"] else ("error" if "error" in o else "**fail**")
        g = o.get("grade")
        grader = "—" if g is None else (tick(g["passed"]) + ("" if g["passed"] else f" {g['notes']}".replace("|", "/")))
        lines.append(
            f"| {o['scenario']} | {tick(s['service'])} | {tick(s['cause'])} | {tick(s['tools'])} | {grader} | {result} | "
            f"{tick(o.get('restored'))} | {', '.join(o.get('tools_called', [])) or '—'} | "
            f"{o.get('tokens', 0):,} | {o.get('seconds', 0)}s |")
    passed = sum(o["score"]["passed"] for o in outcomes)
    lines.append(f"\n{passed}/{len(outcomes)} passed.\n")
    with open(results, "a") as f:
        f.write("\n".join(lines))


# --- Main ---

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_port_forward():
    port = _free_port()
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", "monitoring", "svc/kube-prometheus-stack-prometheus", f"{port}:9090"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, f"http://127.0.0.1:{port}"
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("kubectl port-forward to Prometheus did not start")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", "--scenario", action="append", choices=SCENARIOS, help="run only this scenario (repeatable)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    parser.add_argument("--no-record", action="store_true", help="don't write drills/RESULTS.md")
    parser.add_argument("--no-grader", action="store_true", help="keyword scoring only (no LLM grader)")
    args = parser.parse_args(argv)

    chosen = [SCENARIOS[n] for n in (args.scenario or SCENARIOS)]
    if args.list:
        for s in SCENARIOS.values():
            print(f"{s.name:16} {s.description}")
        return 0

    print("Drills will break and then restore these deployments in boutique:")
    for s in chosen:
        print(f"  - {s.name}: {s.description}")
    if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
        return 1

    forward = None
    if not os.getenv("PROMETHEUS_URL"):
        forward, os.environ["PROMETHEUS_URL"] = start_port_forward()

    import agent
    import k8s

    apps, core, custom = k8s.apps_api(), k8s.core_api(), k8s.custom_api()
    kira = agent.KiraAgent()
    grader = None if args.no_grader else LLMGrader(kira.bedrock, os.getenv("KIRA_GRADER_MODEL", agent.MODEL_ID))
    outcomes = []
    try:
        with ArgoPause(custom):
            for s in chosen:
                o = run_scenario(s, apps, core, kira, grader=grader)
                outcomes.append(o)
                verdict = "PASS" if o["score"]["passed"] else "FAIL"
                print(f"   {verdict} {o['score']} grader={o.get('grade')} restored={o.get('restored')} {o.get('error', '')}\n")
    finally:
        if forward:
            forward.terminate()

    if outcomes and not args.no_record:
        record(outcomes, agent.MODEL_ID)
        print(f"Results appended to {DRILLS_DIR / 'RESULTS.md'}")
    return 0 if outcomes and all(o["score"]["passed"] and o.get("restored") for o in outcomes) else 2


if __name__ == "__main__":
    sys.exit(main())
