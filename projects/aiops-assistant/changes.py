"""
Kira's fetch_recent_changes tool (#10): what changed in a time window, so Kira
can link a symptom to the change that caused it.

Read-only. Returns, newest first:
- Kubernetes events in `boutique` (the API server keeps them ~1 hour)
- deployment state, rollout history (ReplicaSet revisions), and who changed
  each deployment's replicas or pod template, and which fields (managedFields)
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
        "failures; kept about 1 hour), deployment rollouts and who changed each deployment's replicas or pod "
        "template (image, command, ...), "
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


def _container_fields(template_fields):
    """Container field names (image, command, env, ...) set in a managedFields pod template."""
    names = set()
    for key, container in template_fields.get("f:spec", {}).get("f:containers", {}).items():
        if key.startswith("k:"):
            names.update(f[2:] for f in container if f.startswith("f:") and f != "f:name")
    return sorted(names)


def _changed_by(dep, since):
    """Who changed replicas or the pod template within the window, and which fields (from managedFields).

    A manager's time is its latest write to this deployment; Argo CD's
    `argocd-controller` rewrites the whole manifest on every sync. Changes made
    through the scale endpoint (`kubectl scale`) are not recorded here: see
    _replicas_set_via_scale().
    """
    out = []
    for m in dep.metadata.managed_fields or []:
        if not m.time or m.time < since or m.subresource == "status":
            continue
        spec = (m.fields_v1 or {}).get("f:spec", {})
        fields = []
        if "f:replicas" in spec:
            fields.append("replicas")
        if "f:template" in spec:
            fields += _container_fields(spec["f:template"]) or ["template"]
        if fields:
            out.append({"manager": m.manager, "subresource": m.subresource, "time": _iso(m.time), "fields": fields})
    return sorted(out, key=lambda x: x["time"], reverse=True)


def _replicas_set_via_scale(dep):
    """True if no field manager owns spec.replicas.

    Scaling through the scale endpoint (kubectl scale, an autoscaler, or an API
    client) drops the previous owner without recording a new one, so who scaled
    is unknown; the ScalingReplicaSet event still gives the time. (Seen on EKS
    1.34: a manifest apply or patch of spec.replicas does record its manager.)
    """
    return not any("f:replicas" in (m.fields_v1 or {}).get("f:spec", {}) for m in dep.metadata.managed_fields or [])


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
            "changed_by": _changed_by(d, since),
            "replicas_set_via_scale": _replicas_set_via_scale(d),
        })
    for rs in apps.list_namespaced_replica_set(NAMESPACE).items:
        owner = next((by_uid[r.uid] for r in rs.metadata.owner_references or [] if r.uid in by_uid), None)
        created = rs.metadata.creation_timestamp
        if owner is None or created is None or created < since:
            continue
        containers = rs.spec.template.spec.containers
        rollout = {
            "deployment": owner,
            "revision": (rs.metadata.annotations or {}).get(REVISION_ANNOTATION),
            "created": _iso(created),
            "images": [c.image.rsplit("/", 1)[-1] for c in containers],
        }
        commands = [" ".join(c.command) for c in containers if c.command]
        if commands:
            rollout["commands"] = commands  # overrides the image's own entrypoint
        rollouts.append(rollout)
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
