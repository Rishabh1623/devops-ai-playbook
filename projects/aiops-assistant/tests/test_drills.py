"""
Tests for the incident drill script (#9). No cluster or Bedrock needed.

    cd projects/aiops-assistant && python -m unittest discover tests
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kubernetes import client

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

_spec = importlib.util.spec_from_file_location("run_drills", APP_DIR / "scripts" / "run_drills.py")
drills = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drills)

from agent import ChatResult, PendingAction  # noqa: E402

S = drills.SCENARIOS


class ScoreTest(unittest.TestCase):
    def test_good_answers_pass(self):
        cases = {
            "scaled_to_zero": "The orders deployment was scaled down to 0 by kubectl at 11:40.",
            "crash_loop": "product-service pods are in CrashLoopBackOff: the container exits with code 1.",
            "bad_image": "user-service is stuck in ImagePullBackOff because the image tag doesn't exist.",
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.assertTrue(drills.score(S[name], answer, ["fetch_recent_changes"])["passed"])

    def test_wrong_service_cause_or_tools_fail(self):
        s = S["scaled_to_zero"]
        self.assertFalse(drills.score(s, "order-service was scaled to 0", ["fetch_recent_changes"])["service"])
        self.assertFalse(drills.score(s, "orders is out of memory", ["fetch_recent_changes"])["cause"])
        self.assertFalse(drills.score(s, "orders scaled to 0", ["fetch_logs"])["tools"])
        self.assertFalse(drills.score(s, None, [])["passed"])

    def test_service_name_is_not_matched_inside_longer_names(self):
        self.assertFalse(drills.score(S["scaled_to_zero"], "see order-service and orders-db", [])["service"])
        self.assertTrue(drills.score(S["scaled_to_zero"], "the `orders` deployment", [])["service"])


def deployment(name="orders", replicas=1, available=1, image="123.dkr.ecr/orders:abc"):
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, generation=2),
        spec=client.V1DeploymentSpec(
            replicas=replicas, selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": name}),
                spec=client.V1PodSpec(containers=[client.V1Container(name=name, image=image)]))),
        status=client.V1DeploymentStatus(observed_generation=2, replicas=available, updated_replicas=available,
                                         available_replicas=available))


def fake_apps(states):
    """read_namespaced_deployment returns each state in turn, then repeats the last."""
    apps = mock.MagicMock()
    apps.api_client = client.ApiClient()
    seq = list(states)
    apps.read_namespaced_deployment.side_effect = lambda *a: seq.pop(0) if len(seq) > 1 else seq[0]
    return apps


class FakeKira:
    def __init__(self, result=None, error=None, tools=("fetch_recent_changes",)):
        self.result, self.error, self.tools = result, error, tools

    def chat(self, history, on_tool_call=None, budget=None, session_id=None):
        for t in self.tools:
            on_tool_call(t, {})
        budget.add({"inputTokens": 1000, "outputTokens": 50})
        if self.error:
            raise self.error
        return self.result


class RunScenarioTest(unittest.TestCase):
    def setUp(self):
        mock.patch.object(drills, "POLL_SECONDS", 0).start()
        self.addCleanup(mock.patch.stopall)

    def test_scaled_to_zero_end_to_end(self):
        # snapshot, inject check (0 replicas), then healthy after restore
        apps = fake_apps([deployment(), deployment(replicas=0, available=0), deployment()])
        kira = FakeKira(ChatResult(text="orders was scaled to 0 replicas by kubectl"))

        o = drills.run_scenario(S["scaled_to_zero"], apps, mock.MagicMock(), kira, log=lambda *_: None)

        apps.patch_namespaced_deployment_scale.assert_called_once_with(
            "orders", "boutique", {"spec": {"replicas": 0}}, field_manager="kubectl")
        patch = apps.patch_namespaced_deployment.call_args.args[2]
        self.assertEqual([p["path"] for p in patch], ["/spec/template", "/spec/replicas"])
        self.assertEqual(patch[1]["value"], 1)
        self.assertTrue(o["score"]["passed"])
        self.assertTrue(o["restored"])
        self.assertEqual(o["tokens"], 1050)

    def test_restores_even_when_kira_fails(self):
        apps = fake_apps([deployment(), deployment(replicas=0, available=0), deployment()])
        o = drills.run_scenario(S["scaled_to_zero"], apps, mock.MagicMock(),
                                FakeKira(error=RuntimeError("bedrock down")), log=lambda *_: None)

        self.assertIn("bedrock down", o["error"])
        self.assertFalse(o["score"]["passed"])
        apps.patch_namespaced_deployment.assert_called_once()  # restore ran
        self.assertTrue(o["restored"])

    def test_restores_when_failure_never_shows(self):
        apps = fake_apps([deployment()])  # never reaches 0
        with mock.patch.object(drills, "FAILURE_TIMEOUT_SECONDS", 0):
            o = drills.run_scenario(S["scaled_to_zero"], apps, mock.MagicMock(), FakeKira(), log=lambda *_: None)
        self.assertIn("timed out", o["error"])
        apps.patch_namespaced_deployment.assert_called_once()

    def test_proposed_fix_is_recorded_not_run(self):
        apps = fake_apps([deployment(), deployment(replicas=0, available=0), deployment()])
        pending = PendingAction("t1", "scale_deployment", {"deployment": "orders", "replicas": 1,
                                                            "reason": "orders was scaled to 0 by kubectl"})
        with mock.patch("remediation.execute") as execute:
            o = drills.run_scenario(S["scaled_to_zero"], apps, mock.MagicMock(), FakeKira(ChatResult(pending=pending)),
                                    log=lambda *_: None)
        execute.assert_not_called()
        self.assertEqual(o["proposed_fix"]["action"], "scale_deployment")
        self.assertTrue(o["score"]["passed"])

    def test_bad_image_and_crash_loop_inject(self):
        apps = fake_apps([deployment("user-service", image="123.dkr.ecr/user-service:abc")])
        S["bad_image"].inject(apps, apps.read_namespaced_deployment())
        container = apps.patch_namespaced_deployment.call_args.args[2]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], "123.dkr.ecr/user-service:drill-tag-does-not-exist")

        S["crash_loop"].inject(apps, deployment("product-service"))
        container = apps.patch_namespaced_deployment.call_args.args[2]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"][0], "node")
        self.assertIn("process.exit(1)", container["command"][2])

    def test_bad_pod_failure_detection(self):
        def pod(reason):
            return client.V1Pod(status=client.V1PodStatus(container_statuses=[client.V1ContainerStatus(
                name="c", image="i", image_id="", ready=False, restart_count=2,
                state=client.V1ContainerState(waiting=client.V1ContainerStateWaiting(reason=reason)))]))
        apps = fake_apps([deployment("product-service")])
        core = mock.MagicMock()
        core.list_namespaced_pod.return_value = client.V1PodList(items=[pod("ContainerCreating")])
        self.assertFalse(S["crash_loop"].failure_visible(apps, core))
        core.list_namespaced_pod.return_value = client.V1PodList(items=[pod("CrashLoopBackOff")])
        self.assertTrue(S["crash_loop"].failure_visible(apps, core))
        core.list_namespaced_pod.return_value = client.V1PodList(items=[pod("ImagePullBackOff")])
        self.assertTrue(S["bad_image"].failure_visible(apps, core))


def grader_reply(text):
    bedrock = mock.MagicMock()
    bedrock.converse.return_value = {"output": {"message": {"content": [{"text": text}]}}, "usage": {"totalTokens": 400}}
    return bedrock


class LLMGraderTest(unittest.TestCase):
    def test_parses_verdict_and_sends_ground_truth(self):
        bedrock = grader_reply('Sure: {"service": true, "cause": true, "trigger": false, "notes": "blamed commit 807ccc8"}')
        grade = drills.LLMGrader(bedrock, "m")(S["scaled_to_zero"], "orders was scaled to 0 by commit 807ccc8")

        self.assertEqual((grade["service"], grade["cause"], grade["trigger"], grade["passed"]), (True, True, False, False))
        self.assertEqual(grade["notes"], "blamed commit 807ccc8")
        prompt = bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
        self.assertIn("kubectl scale deployment orders --replicas=0", prompt)
        self.assertIn("commit 807ccc8", prompt)

    def test_unparseable_output_fails(self):
        grade = drills.LLMGrader(grader_reply("I think it's fine"), "m")(S["bad_image"], "x")
        self.assertFalse(grade["passed"])
        self.assertIn("unparseable", grade["notes"])

    def test_every_scenario_has_ground_truth(self):
        self.assertTrue(all(s.truth for s in S.values()))

    def test_grader_can_fail_a_keyword_pass(self):
        apps = fake_apps([deployment(), deployment(replicas=0, available=0), deployment()])
        kira = FakeKira(ChatResult(text="orders was scaled to 0 replicas by commit 807ccc8"))
        grader = mock.MagicMock(return_value={"passed": False, "notes": "wrong trigger"})
        with mock.patch.object(drills, "POLL_SECONDS", 0):
            o = drills.run_scenario(S["scaled_to_zero"], apps, mock.MagicMock(), kira, log=lambda *_: None, grader=grader)
        self.assertTrue(all(o["score"][k] for k in ("service", "cause", "tools")))
        self.assertFalse(o["score"]["passed"])
        self.assertTrue(o["restored"])

    def test_injections_use_kubectl_field_manager(self):
        apps = fake_apps([deployment()])
        S["scaled_to_zero"].inject(apps, deployment())
        self.assertEqual(apps.patch_namespaced_deployment_scale.call_args.kwargs["field_manager"], "kubectl")
        S["bad_image"].inject(apps, deployment("user-service"))
        self.assertEqual(apps.patch_namespaced_deployment.call_args.kwargs["field_manager"], "kubectl")
        drills.restore(apps, "orders", {"replicas": 1, "template": {}})
        self.assertEqual(apps.patch_namespaced_deployment.call_args.kwargs["field_manager"], "kira-drill-restore")


class ArgoPauseTest(unittest.TestCase):
    def test_pauses_and_restores_automated_sync(self):
        custom = mock.MagicMock()
        custom.get_namespaced_custom_object.return_value = {"spec": {"syncPolicy": {
            "automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]}}}
        with self.assertRaises(RuntimeError):
            with drills.ArgoPause(custom):
                paused = custom.patch_namespaced_custom_object.call_args.args[-1]
                self.assertEqual(paused, {"spec": {"syncPolicy": {"automated": None}}})
                raise RuntimeError("drill crashed")
        restored = custom.patch_namespaced_custom_object.call_args.args[-1]
        self.assertEqual(restored, {"spec": {"syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}})

    def test_leaves_manual_sync_alone(self):
        custom = mock.MagicMock()
        custom.get_namespaced_custom_object.return_value = {"spec": {}}
        with drills.ArgoPause(custom):
            pass
        custom.patch_namespaced_custom_object.assert_not_called()


class RecordTest(unittest.TestCase):
    def test_writes_table_and_jsonl(self):
        outcomes = [
            {"scenario": "scaled_to_zero", "score": {"service": True, "cause": True, "tools": True, "passed": True},
             "restored": True, "tools_called": ["fetch_recent_changes"], "tokens": 12000, "seconds": 40, "answer": "a",
             "grade": {"passed": True, "notes": ""}},
            {"scenario": "bad_image", "score": {"service": True, "cause": False, "tools": True, "passed": False},
             "restored": True, "tools_called": [], "tokens": 0, "seconds": 5, "error": "x",
             "grade": {"passed": False, "notes": "blamed a commit | wrongly"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "drills"
            drills.record(outcomes, "model-x", d, now=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
            md = (d / "RESULTS.md").read_text()
            rows = [json.loads(line) for line in (d / "results.jsonl").read_text().splitlines()]

        self.assertIn("## 2026-10-10 12:00 UTC · `model-x`", md)
        self.assertIn("| scaled_to_zero | ✅ | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_recent_changes | 12,000 | 40s |", md)
        self.assertIn("| bad_image | ✅ | ❌ | ✅ | ❌ blamed a commit / wrongly | error |", md)
        self.assertIn("1/2 passed.", md)
        self.assertEqual([r["model"] for r in rows], ["model-x", "model-x"])


if __name__ == "__main__":
    unittest.main()
