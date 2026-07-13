"""Driver Lambda for the Bedrock -> Bronto demo.

Invoked by an EventBridge rule on a schedule; POSTs a rotating prompt to the
demo app's /chat endpoint (via the ALB) so a steady stream of traces, logs and
metrics flows into Bronto continuously. Telemetry is emitted by the FastAPI
app itself - this driver only generates traffic.
"""

from __future__ import annotations

import json
import os
import random
import urllib.error
import urllib.request

APP_BASE_URL = os.environ["APP_BASE_URL"].rstrip("/")

# Varied prompts so the generated traces/logs stay interesting: different
# lengths, topics and output sizes exercise the token/latency metrics.
_PROMPTS = (
    "Explain what OpenTelemetry is in two sentences.",
    "Give me three tips for writing useful structured log events.",
    "What's the difference between traces, metrics and logs?",
    "Summarise the CAP theorem for a new engineer.",
    "Write a haiku about observability.",
    "Why do distributed systems need correlation IDs?",
    "List five common causes of high p99 latency in a web service.",
    "Explain exponential backoff and when to use jitter.",
    "What is an SLO and how does it differ from an SLA?",
    "Describe the sidecar pattern in one paragraph.",
    "How does sampling affect trace data, and when is head sampling enough?",
    "Give a one-line definition of cardinality in the context of metrics.",
    "What should an on-call engineer check first when error rates spike?",
    "Explain blue/green versus rolling deployments in a few sentences.",
    "Why is 'grep-able' logging not enough for modern systems?",
)

# Occasionally set a system prompt so the gen_ai spans vary too.
_SYSTEMS = (
    None,
    None,
    None,
    "You are a concise SRE mentor. Answer in at most three sentences.",
    "You are an enthusiastic observability evangelist. Keep it short.",
)


def handler(event, context):
    prompt = random.choice(_PROMPTS)
    system = random.choice(_SYSTEMS)
    body: dict = {"prompt": prompt}
    if system:
        body["system"] = system

    req = urllib.request.Request(
        f"{APP_BASE_URL}/chat",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read() or b"{}")
            status = resp.status
    except urllib.error.HTTPError as exc:
        result = {"error": exc.read().decode(errors="replace")[:500]}
        status = exc.code

    summary = {
        "ok": 200 <= status < 300,
        "status": status,
        "prompt": prompt,
        "system": system,
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "latency_ms": result.get("latency_ms"),
        "error": result.get("error"),
    }
    print(json.dumps(summary))
    return summary
