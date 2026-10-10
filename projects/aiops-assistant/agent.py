"""
Kira agent loop — Claude on Bedrock (Converse API) calling the aiops tools.

Replaces the Bedrock Agents (classic) agent, which can't be created on accounts
without prior Bedrock Agents usage. Tools are built from schemas/*.json and each
tool receives the same event shape a Bedrock Agent action group would send, so
the tool code in lambda/ is unchanged.

fetch_logs runs as a Lambda. fetch_metrics and fetch_service_health run inside
this process instead, so they reach Prometheus at its private in-cluster
(ClusterIP) address rather than through a public load balancer.
"""

import functools
import importlib.util
import json
import os
from pathlib import Path

import boto3

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
MAX_TOKENS = 4096
MAX_TOOL_ROUNDS = 8

SCHEMA_DIR = Path(__file__).parent / "schemas"
LAMBDA_DIR = Path(__file__).parent / "lambda"

# tool name (as Kira's instructions refer to it) -> (schema file, backend)
# backend is "lambda:<function name>" or "local:<directory under lambda/>"
TOOLS = {
    "fetch_logs": ("fetch_logs.json", "lambda:aiops-fetch-logs"),
    "fetch_metrics": ("fetch_metrics.json", "local:fetch_metrics"),
    "fetch_service_health": ("fetch_health.json", "local:fetch_health"),
}

SYSTEM_PROMPT = """You are Kira, a senior Site Reliability Engineer with 12 years of experience managing large-scale production systems on AWS. You have deep expertise in distributed systems, database performance tuning, container orchestration, and incident response.

You think like a real SRE during an incident — calm, methodical, and data-driven. You never guess. You always look at the data first before drawing conclusions.

You have 3 tools: fetch_logs (CloudWatch Logs), fetch_metrics (Prometheus pod metrics), and fetch_service_health (EKS cluster, node group, and pod health).

When an engineer comes with a problem:
Step 1: Understand the symptom.
Step 2: Form a hypothesis.
Step 3: Gather evidence using your tools.
Step 4: Diagnose by correlating the data across logs, metrics, and service health.
Step 5: Respond with root cause, evidence summary, immediate fix, and prevention steps.

Always cite specific log entries or metric values when drawing conclusions. Be concise but thorough."""


def _load_tools():
    """Build Converse toolSpecs from the OpenAPI schemas used by the old action groups."""
    specs, routes = [], {}
    for name, (schema_file, backend) in TOOLS.items():
        schema = json.loads((SCHEMA_DIR / schema_file).read_text())
        api_path, ops = next(iter(schema["paths"].items()))
        method, op = next(iter(ops.items()))

        properties, required = {}, []
        for param in op.get("parameters", []):
            prop = dict(param.get("schema", {"type": "string"}))
            prop["description"] = param.get("description", "")
            properties[param["name"]] = prop
            if param.get("required"):
                required.append(param["name"])

        specs.append({"toolSpec": {
            "name": name,
            "description": op.get("description") or op.get("summary", ""),
            "inputSchema": {"json": {"type": "object", "properties": properties, "required": required}},
        }})
        routes[name] = (backend, api_path, method.upper())
    return specs, routes


TOOL_SPECS, TOOL_ROUTES = _load_tools()


@functools.cache
def _local_handler(tool_dir):
    """Load lambda/<tool_dir>/lambda_function.py and return its lambda_handler."""
    path = LAMBDA_DIR / tool_dir / "lambda_function.py"
    spec = importlib.util.spec_from_file_location(f"tools.{tool_dir}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.lambda_handler


class KiraAgent:
    def __init__(self, session=None):
        session = session or boto3.Session(region_name=AWS_REGION)
        self.bedrock = session.client("bedrock-runtime", region_name=AWS_REGION)
        self.lambda_client = session.client("lambda", region_name=AWS_REGION)

    def _call_tool(self, name, tool_input):
        """Run the tool with a Bedrock-Agent-style event and return its JSON body."""
        if name not in TOOL_ROUTES:
            return {"status": "error", "message": f"Unknown tool: {name}"}
        backend, api_path, method = TOOL_ROUTES[name]
        kind, target = backend.split(":", 1)
        event = {
            "messageVersion": "1.0",
            "actionGroup": name,
            "apiPath": api_path,
            "httpMethod": method,
            "parameters": [
                {"name": k, "type": "string", "value": str(v)}
                for k, v in tool_input.items() if v is not None
            ],
        }
        try:
            if kind == "local":
                payload = _local_handler(target)(event, None)
            else:
                resp = self.lambda_client.invoke(FunctionName=target, Payload=json.dumps(event))
                payload = json.loads(resp["Payload"].read())
                if resp.get("FunctionError"):
                    return {"status": "error", "message": payload.get("errorMessage", str(payload))}
            body = payload["response"]["responseBody"]["application/json"]["body"]
            return json.loads(body)
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    def chat(self, messages, on_tool_call=None):
        """
        Run one user turn. `messages` is the Converse history ending with the new
        user message; it is extended in place with the assistant/tool turns.
        Returns the final answer text.
        """
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.bedrock.converse(
                modelId=MODEL_ID,
                system=[{"text": SYSTEM_PROMPT}],
                messages=messages,
                toolConfig={"tools": TOOL_SPECS},
                inferenceConfig={"maxTokens": MAX_TOKENS},
            )
            message = resp["output"]["message"]
            messages.append(message)

            if resp["stopReason"] != "tool_use":
                return "".join(b["text"] for b in message["content"] if "text" in b)

            results = []
            for block in message["content"]:
                if "toolUse" not in block:
                    continue
                use = block["toolUse"]
                if on_tool_call:
                    on_tool_call(use["name"], use["input"])
                result = self._call_tool(use["name"], use["input"])
                results.append({"toolResult": {
                    "toolUseId": use["toolUseId"],
                    "content": [{"json": result}],
                    "status": "error" if result.get("status") == "error" else "success",
                }})
            messages.append({"role": "user", "content": results})

        text = "⚠️ Stopped after too many tool calls without a final answer."
        # Keep the history valid for the next turn (must not end on a tool-result message).
        messages.append({"role": "assistant", "content": [{"text": text}]})
        return text
