"""
Tests for Kira's fetch_recent_changes tool (#10).

    cd projects/aiops-assistant && python -m unittest discover tests
"""

import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kubernetes import client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import changes  # noqa: E402
from guardrails import TokenBudget  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def ago(minutes):
    return NOW - timedelta(minutes=minutes)


def event(name, kind, reason, message, minutes_ago, type_="Normal"):
    return client.CoreV1Event(
        metadata=client.V1ObjectMeta(name=f"{name}.1"),
        involved_object=client.V1ObjectReference(kind=kind, name=name),
        reason=reason, message=message, type=type_, count=1, last_timestamp=ago(minutes_ago))


def managed(manager, minutes_ago, subresource=None, owns_replicas=True):
    fields = {"f:spec": {"f:replicas": {}}} if owns_replicas else {"f:metadata": {}}
    return client.V1ManagedFieldsEntry(manager=manager, operation="Update", subresource=subresource,
                                       time=ago(minutes_ago), fields_v1=fields)


def deployment(name, uid, replicas, available, managed_fields=()):
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, uid=uid, annotations={"deployment.kubernetes.io/revision": "4"},
                                     managed_fields=list(managed_fields)),
        spec=client.V1DeploymentSpec(replicas=replicas, selector=client.V1LabelSelector(),
                                     template=client.V1PodTemplateSpec()),
        status=client.V1DeploymentStatus(available_replicas=available))


def replicaset(owner_uid, revision, minutes_ago, image):
    return client.V1ReplicaSet(
        metadata=client.V1ObjectMeta(
            annotations={"deployment.kubernetes.io/revision": str(revision)}, creation_timestamp=ago(minutes_ago),
            owner_references=[client.V1OwnerReference(api_version="apps/v1", kind="Deployment", name="x", uid=owner_uid)]),
        spec=client.V1ReplicaSetSpec(selector=client.V1LabelSelector(), template=client.V1PodTemplateSpec(
            spec=client.V1PodSpec(containers=[client.V1Container(name="c", image=image)]))))


ARGO_APP = {"status": {
    "sync": {"status": "Synced", "revision": "abc1234fffffff"},
    "health": {"status": "Degraded"},
    "operationState": {"phase": "Succeeded", "finishedAt": "2026-10-10T11:00:00Z", "message": "successfully synced"},
    "history": [
        {"revision": "old0000", "deployedAt": "2026-10-09T01:00:00Z"},  # outside window
        {"revision": "def5678fffffff", "deployedAt": "2026-10-10T10:00:00Z"},
        {"revision": "abc1234fffffff", "deployedAt": "2026-10-10T11:00:00Z"},
    ],
}}

COMMITS = [
    {"sha": "abc1234fffffff", "commit": {"message": "ci: update image tags\n\nbody", "author": {"name": "github-actions"},
                                         "committer": {"date": "2026-10-10T10:58:00Z"}}},
    {"sha": "def5678fffffff", "commit": {"message": "feat: something", "author": {"name": "Dev"},
                                         "committer": {"date": "2026-10-10T09:50:00Z"}}},
]


def fake_clients():
    """`orders` scaled to 0 by kubectl 20 minutes ago (the #10 demo scenario)."""
    core = mock.MagicMock()
    core.list_namespaced_event.return_value = client.CoreV1EventList(items=[
        event("orders", "Deployment", "ScalingReplicaSet", "Scaled down replica set orders-7d9f to 0 from 1", 20),
        event("orders-7d9f-abcde", "Pod", "Killing", "Stopping container orders", 20),
        event("gateway-55c8-xyz12", "Pod", "BackOff", "Back-off restarting failed container", 5, "Warning"),
        event("orders", "Deployment", "ScalingReplicaSet", "too old", 60 * 24),  # outside window
    ])
    apps = mock.MagicMock()
    apps.list_namespaced_deployment.return_value = client.V1DeploymentList(items=[
        deployment("orders", "u-orders", 0, 0, [
            managed("argocd-controller", 120),
            managed("kubectl", 20, subresource="scale"),
            managed("kube-controller-manager", 19, subresource="status", owns_replicas=False),
        ]),
        deployment("gateway", "u-gateway", 1, 1),
    ])
    apps.list_namespaced_replica_set.return_value = client.V1ReplicaSetList(items=[
        replicaset("u-orders", 4, 120, "123.dkr.ecr.us-east-1.amazonaws.com/orders:abc1234"),
        replicaset("u-orders", 3, 60 * 24, "orders:old"),  # outside window
        replicaset("u-gateway", 7, 120, "gateway:abc1234"),
        replicaset("u-other", 1, 10, "other:1"),  # not owned by a listed deployment
    ])
    custom = mock.MagicMock()
    custom.get_namespaced_custom_object.return_value = ARGO_APP
    return {"apps": apps, "core": core, "custom": custom}


def github_response(commits):
    return mock.patch("urllib.request.urlopen", return_value=mock.MagicMock(
        __enter__=lambda s: io.BytesIO(json.dumps(commits).encode()), __exit__=lambda *a: False))


class FetchRecentChangesTest(unittest.TestCase):
    def run_tool(self, **kwargs):
        with github_response(COMMITS) as urlopen:
            result = changes.fetch_recent_changes(**{"hours_back": 6, "now": NOW, **fake_clients(), **kwargs})
        return result, urlopen

    def test_demo_scenario_links_zero_replicas_to_scale_event(self):
        result, _ = self.run_tool()

        orders = next(d for d in result["deployments"] if d["name"] == "orders")
        self.assertEqual((orders["replicas"], orders["available"]), (0, 0))
        self.assertEqual(orders["scaled_by"][0], {"manager": "kubectl", "subresource": "scale", "time": "2026-10-10T11:40:00Z"})
        scale = next(e for e in result["events"] if e["reason"] == "ScalingReplicaSet")
        self.assertEqual(scale["object"], "Deployment/orders")
        self.assertIn("to 0 from 1", scale["message"])
        self.assertEqual(scale["time"], "2026-10-10T11:40:00Z")

    def test_events_newest_first_and_inside_window(self):
        result, _ = self.run_tool()
        times = [e["time"] for e in result["events"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertNotIn("too old", json.dumps(result["events"]))
        self.assertEqual(result["events"][0]["reason"], "BackOff")

    def test_deployment_filter(self):
        result, _ = self.run_tool(deployment="orders")
        self.assertTrue(all("orders" in e["object"] for e in result["events"]))
        self.assertEqual([d["name"] for d in result["deployments"]], ["orders"])
        self.assertEqual({r["deployment"] for r in result["rollouts"]}, {"orders"})

    def test_rollouts_only_owned_and_in_window(self):
        result, _ = self.run_tool()
        self.assertEqual(result["rollouts"], [
            {"deployment": "orders", "revision": "4", "created": "2026-10-10T10:00:00Z", "images": ["orders:abc1234"]},
            {"deployment": "gateway", "revision": "7", "created": "2026-10-10T10:00:00Z", "images": ["gateway:abc1234"]},
        ])

    def test_argocd_history(self):
        result, _ = self.run_tool()
        self.assertEqual(result["argocd"]["deployed_revision"], "abc1234")
        self.assertEqual(result["argocd"]["health"], "Degraded")
        self.assertEqual([s["revision"] for s in result["argocd"]["syncs"]], ["abc1234", "def5678"])

    def test_commits_mark_deployed_and_query_window(self):
        result, urlopen = self.run_tool()
        self.assertEqual(result["commits"][0], {"sha": "abc1234", "date": "2026-10-10T10:58:00Z",
                                                "author": "github-actions", "message": "ci: update image tags",
                                                "deployed": True})
        self.assertFalse(result["commits"][1]["deployed"])
        url = urlopen.call_args.args[0].full_url
        self.assertIn("sha=project-demo", url)
        self.assertIn("since=2026-10-10T06%3A00%3A00Z", url)

    def test_one_failing_source_does_not_hide_the_others(self):
        clients = fake_clients()
        clients["custom"].get_namespaced_custom_object.side_effect = client.ApiException(status=403, reason="Forbidden")
        with github_response(COMMITS):
            result = changes.fetch_recent_changes(hours_back=6, now=NOW, **clients)

        self.assertIn("Forbidden", result["argocd"]["error"])
        self.assertTrue(result["events"])
        self.assertNotIn("deployed", result["commits"][0])
        json.dumps(result)  # still serialisable for the model

    def test_failure_in_two_value_source(self):
        clients = fake_clients()
        clients["apps"].list_namespaced_deployment.side_effect = RuntimeError("boom")
        with github_response(COMMITS):
            result = changes.fetch_recent_changes(hours_back=6, now=NOW, **clients)
        self.assertIn("boom", result["deployments"]["error"])
        self.assertIn("boom", result["rollouts"]["error"])
        json.dumps(result)

    def test_hours_are_clamped(self):
        for given, expected in [(0, 1), (500, 48), ("abc", 6), ("3", 3)]:
            with self.subTest(given=given):
                result, _ = self.run_tool(hours_back=given)
                self.assertEqual(result["window_hours"], expected)


class AgentIntegrationTest(unittest.TestCase):
    def test_run_ignores_parameters_the_model_should_not_set(self):
        with mock.patch.object(changes, "fetch_recent_changes", return_value={"status": "ok"}) as fetch:
            changes.run({"hours_back": 2, "deployment": "orders", "apps": "evil", "now": "x"})
        fetch.assert_called_once_with(2, "orders")

    def test_agent_routes_tool_in_process(self):
        with mock.patch("boto3.Session"):
            kira = agent.KiraAgent()
        with mock.patch.dict(agent.PYTHON_TOOLS, {"fetch_recent_changes": lambda i: {"status": "ok", "got": i}}):
            result = kira._call_tool("fetch_recent_changes", {"hours_back": 1})
        self.assertEqual(result, {"status": "ok", "got": {"hours_back": 1}})
        kira.lambda_client.invoke.assert_not_called()

    def test_tool_offered_to_model_and_prompt_says_use_it_early(self):
        names = [t["toolSpec"]["name"] for t in agent.READ_TOOL_SPECS]
        self.assertIn("fetch_recent_changes", names)
        self.assertIn("Early on, call fetch_recent_changes", agent.SYSTEM_PROMPT)



@unittest.skipUnless(os.getenv("KIRA_LIVE_TESTS") == "1", "set KIRA_LIVE_TESTS=1 to call Bedrock")
class LiveChangeCorrelationTest(unittest.TestCase):
    """Demo (#10): Kira links orders at 0 replicas to the scale event and its time (real Claude, fake tools)."""

    def test_links_zero_replicas_to_scale_event(self):
        with github_response(COMMITS):
            recent = changes.fetch_recent_changes(hours_back=6, now=NOW, **fake_clients())
        kira = agent.KiraAgent()
        calls = []

        def fake_tool(name, tool_input):
            calls.append(name)
            if name == "fetch_recent_changes":
                return recent
            if name == "fetch_service_health":
                return {"status": "success", "overall_healthy": False, "details": {"eks": {
                    "unhealthy_deployments": [{"name": "orders", "issue": "scaled to zero"}]}}}
            if name == "fetch_logs":
                return {"status": "ok", "events": [
                    "2026-10-10T11:41:05Z ERROR gateway: upstream orders unavailable (503)"]}
            return {"status": "no_data"}

        kira._call_tool = fake_tool
        history = [{"role": "user", "content": [{"text": "Customers can't place orders since about 11:40 UTC. Why?"}]}]
        result = kira.chat(history, budget=TokenBudget(limit=80000))
        answer = result.text or json.dumps(result.pending.input)
        print("\n--- Kira ---\n" + answer)

        self.assertIn("fetch_recent_changes", calls)
        self.assertLess(calls.index("fetch_recent_changes"), 3, "should check recent changes early")
        self.assertIn("orders", answer.lower())
        self.assertIn("11:40", answer)
        self.assertRegex(answer.lower(), r"kubectl|scal")


if __name__ == "__main__":
    unittest.main()
