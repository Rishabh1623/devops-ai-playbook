"""
Kira's fetch_recent_changes tool (#10): what changed in a time window, so Kira
can link a symptom to the change that caused it.

Read-only. Returns, newest first:
- Kubernetes events in `boutique` (the API server keeps them ~1 hour)
- deployment state, rollout history (ReplicaSet revisions), and who last
  scaled each deployment (from managedFields)
- Argo CD sync history for the `boutique` Application
- recent commits on the deployed branch (GitHub API)

Each source is fetched independently; if one fails, its error is returned and
the others still are. Results are capped to keep token use down.
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import k8s
from remediation import NAMESPACE, REVISION_ANNOTATION

ARGOCD_NAMESPACE = "argocd"
ARGOCD_APP = "boutique"
GITHUB_REPO = os.getenv("KIRA_GITHUB_REPO", "Rishabh1623/devops-ai-playbook")
GIT_BRANCH = os.getenv("KIRA_GIT_BRANCH", "project-demo")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # optional: public repo, raises rate limit

DEFAULT_HOURS, MAX_HOURS = 6, 48
MAX_EVENTS, MAX_ROLLOUTS, MAX_SYNCS, MAX_COMMITS = 30, 15, 10, 15

TOOL_SPECS = [{"toolSpec": {
    "name": "fetch_recent_changes",
    "description": (
        "What changed recently in the boutique namespace: Kubernetes events (scaling, restarts, "
        "failures; kept about 1 hour), deployment rollouts and who last scaled each deployment, "
        "Argo CD syncs, and recent git commits. Use early in an investigation to link a symptom "
        "to the change that caused it."
    ),
    "inputSchema": {"json": {"type": "object", "properties": {
        "hours_back": {"type": "integer", "description": f"Time window in hours (default {DEFAULT_HOURS}, max {MAX_HOURS})"},
        "deployment": {"type": "string", "description": "Optional: only events and rollouts for this deployment, e.g. orders"},
    }, "required": []}},
}}]


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _matches(name, deployment):
    """True if an object name belongs to the deployment (pods and ReplicaSets add suffixes)."""
    return deployment is None or name == deployment or name.startswith(f"{deployment}-")


def _events(core, since, deployment):
    out = []
    for e in core.list_namespaced_event(NAMESPACE).items:
        when = e.last_timestamp or e.event_time or e.first_timestamp or e.metadata.creation_timestamp
        obj = e.involved_object
        if when is None or when < since or not _matches(obj.name or "", deployment):
            continue
        out.append({
            "time": _iso(when), "type": e.type, "reason": e.reason,
            "object": f"{obj.kind}/{obj.name}", "message": e.message, "count": e.count or 1,
        })
    out.sort(key=lambda x: x["time"], reverse=True)
    return out[:MAX_EVENTS]


def _scaled_by(dep, since):
    """managedFields entries that own spec.replicas and changed within the window."""
    out = []
    for m in dep.metadata.managed_fields or []:
        owns_replicas = "f:replicas" in (m.fields_v1 or {}).get("f:spec", {})
        if (owns_replicas or m.subresource == "scale") and m.time and m.time >= since:
            out.append({"manager": m.manager, "subresource": m.subresource, "time": _iso(m.time)})
    return sorted(out, key=lambda x: x["time"], reverse=True)


def _deployments(apps, since, deployment):
    deployments, rollouts = [], []
    deps = [d for d in apps.list_namespaced_deployment(NAMESPACE).items if _matches(d.metadata.name, deployment)]
    by_uid = {d.metadata.uid: d.metadata.name for d in deps}
    for d in deps:
        deployments.append({
            "name": d.metadata.name,
            "replicas": d.spec.replicas,
            "available": d.status.available_replicas or 0,
            "revision": (d.metadata.annotations or {}).get(REVISION_ANNOTATION),
            "scaled_by": _scaled_by(d, since),
        })
    for rs in apps.list_namespaced_replica_set(NAMESPACE).items:
        owner = next((by_uid[r.uid] for r in rs.metadata.owner_references or [] if r.uid in by_uid), None)
        created = rs.metadata.creation_timestamp
        if owner is None or created is None or created < since:
            continue
        rollouts.append({
            "deployment": owner,
            "revision": (rs.metadata.annotations or {}).get(REVISION_ANNOTATION),
            "created": _iso(created),
            "images": [c.image.rsplit("/", 1)[-1] for c in rs.spec.template.spec.containers],
        })
    rollouts.sort(key=lambda x: x["created"], reverse=True)
    return deployments, rollouts[:MAX_ROLLOUTS]


def _argocd(custom, since):
    app = custom.get_namespaced_custom_object(
        "argoproj.io", "v1alpha1", ARGOCD_NAMESPACE, "applications", ARGOCD_APP)
    status = app.get("status", {})
    op = status.get("operationState", {})
    syncs = [
        {"revision": h.get("revision", "")[:7], "deployed_at": h.get("deployedAt")}
        for h in status.get("history", [])
        if _parse(h.get("deployedAt")) and _parse(h["deployedAt"]) >= since
    ]
    return {
        "sync_status": status.get("sync", {}).get("status"),
        "health": status.get("health", {}).get("status"),
        "deployed_revision": status.get("sync", {}).get("revision", "")[:7],
        "last_operation": {"phase": op.get("phase"), "finished": op.get("finishedAt"), "message": op.get("message")},
        "syncs": list(reversed(syncs))[:MAX_SYNCS],
    }


def _commits(since):
    query = urllib.parse.urlencode({"sha": GIT_BRANCH, "since": _iso(since), "per_page": MAX_COMMITS})
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/commits?{query}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "kira-aiops",
                 **({"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {})})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return [{
        "sha": c["sha"][:7],
        "date": c["commit"]["committer"]["date"],
        "author": c["commit"]["author"]["name"],
        "message": c["commit"]["message"].splitlines()[0][:120],
    } for c in data]


def fetch_recent_changes(hours_back=DEFAULT_HOURS, deployment=None, apps=None, core=None, custom=None, now=None):
    try:
        hours = min(max(int(hours_back), 1), MAX_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    result = {"status": "ok", "window_hours": hours, "since": _iso(since), "namespace": NAMESPACE}

    def source(keys, fn):
        """Store fn()'s value(s) under keys, or the error under each key if it fails."""
        try:
            values = fn()
            values = values if isinstance(values, tuple) else (values,)
        except Exception as e:
            values = ({"error": f"{type(e).__name__}: {e}"},) * len(keys)
        result.update(zip(keys, values))

    source(("events",), lambda: _events(core or k8s.core_api(), since, deployment))
    source(("deployments", "rollouts"), lambda: _deployments(apps or k8s.apps_api(), since, deployment))
    source(("argocd",), lambda: _argocd(custom or k8s.custom_api(), since))
    source(("commits",), lambda: _commits(since))

    deployed = result["argocd"].get("deployed_revision")
    if isinstance(result["commits"], list) and deployed:
        for c in result["commits"]:
            c["deployed"] = c["sha"] == deployed
    return result


def run(tool_input):
    """Entry point for the agent: only the tool's own parameters, never the test hooks."""
    return fetch_recent_changes(tool_input.get("hours_back", DEFAULT_HOURS), tool_input.get("deployment"))
