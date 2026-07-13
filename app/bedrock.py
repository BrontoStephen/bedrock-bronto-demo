"""Thin wrapper around the Amazon Bedrock Converse API.

Uses an EU cross-region inference profile id (default
``eu.amazon.nova-micro-v1:0``) so traffic stays within the EU geo. The
Converse API is model-agnostic, so swapping ``BEDROCK_MODEL_ID`` to any model
the account can invoke works unchanged. The botocore OTel instrumentation
traces the ``converse`` call automatically; here we add a couple of explicit
GenAI metrics and structured log lines that are convenient to demo in Bronto.
"""

from __future__ import annotations

import json
import logging
import os
import time

import boto3
from opentelemetry import trace

from telemetry import get_meter

log = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "eu-west-1")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0")

_client = boto3.client("bedrock-runtime", region_name=REGION)

# Explicit demo metrics (the botocore instrumentation also emits gen_ai.* metrics;
# these give us simple, named series that are easy to chart in Bronto).
_meter = get_meter()
_token_counter = _meter.create_counter(
    "demo.bedrock.tokens",
    unit="{token}",
    description="Bedrock tokens consumed, split by direction (input/output).",
)
_latency_hist = _meter.create_histogram(
    "demo.bedrock.latency",
    unit="ms",
    description="End-to-end Bedrock Converse latency.",
)
_invocation_counter = _meter.create_counter(
    "demo.bedrock.invocations",
    unit="{call}",
    description="Number of Bedrock Converse calls, split by outcome.",
)


def converse(prompt: str, system: str | None = None) -> dict:
    """Send a single-turn prompt to Bedrock Converse and return a result dict."""
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    kwargs = {
        "modelId": MODEL_ID,
        "messages": messages,
        "inferenceConfig": {"maxTokens": 512, "temperature": 0.7},
    }
    if system:
        kwargs["system"] = [{"text": system}]

    start = time.perf_counter()
    try:
        resp = _client.converse(**kwargs)
    except Exception as exc:  # noqa: BLE001 - we want to record + re-raise
        _invocation_counter.add(1, {"model": MODEL_ID, "outcome": "error"})
        log.error(
            "bedrock.converse",
            extra={
                "event.name": "bedrock.converse",
                "outcome": "error",
                "gen_ai.request.model": MODEL_ID,
                "error.message": str(exc),
            },
        )
        raise

    latency_ms = (time.perf_counter() - start) * 1000.0
    text = resp["output"]["message"]["content"][0]["text"]
    usage = resp.get("usage", {})
    in_tok = usage.get("inputTokens", 0)
    out_tok = usage.get("outputTokens", 0)

    # Prompt/response content in the shape the current GenAI semconv defines
    # (gen_ai.input.messages / gen_ai.output.messages / gen_ai.system_instructions,
    # JSON-encoded), reused below for both the span and the log record.
    finish_reason = resp.get("stopReason")
    input_messages = json.dumps(
        [{"role": "user", "parts": [{"type": "text", "content": prompt}]}]
    )
    output_messages = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": text}],
                "finish_reason": finish_reason or "",
            }
        ]
    )
    system_instructions = (
        json.dumps([{"type": "text", "content": system}]) if system else None
    )

    # Content on the trace as plain span attributes. The botocore
    # instrumentation only emits content as log events - the deprecated
    # per-role gen_ai.*.message / gen_ai.choice pattern - and plain attributes
    # are what Bronto's trace search indexes, so this makes the
    # prompt/response visible in the trace view too (the current span here is
    # the POST /chat server span).
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("gen_ai.input.messages", input_messages)
        span.set_attribute("gen_ai.output.messages", output_messages)
        if system_instructions:
            span.set_attribute("gen_ai.system_instructions", system_instructions)

    _latency_hist.record(latency_ms, {"model": MODEL_ID})
    _token_counter.add(in_tok, {"model": MODEL_ID, "direction": "input"})
    _token_counter.add(out_tok, {"model": MODEL_ID, "direction": "output"})
    _invocation_counter.add(1, {"model": MODEL_ID, "outcome": "success"})

    # Structured log: short event name as the body, data as queryable attributes
    # (the OTel LoggingHandler maps `extra` into log-record attributes, which
    # arrive in Bronto as first-class fields rather than text in the message).
    log_attrs = {
        "event.name": "bedrock.converse",
        "outcome": "success",
        "gen_ai.request.model": MODEL_ID,
        "gen_ai.usage.input_tokens": in_tok,
        "gen_ai.usage.output_tokens": out_tok,
        "gen_ai.response.finish_reasons": [finish_reason] if finish_reason else [],
        "latency_ms": round(latency_ms, 1),
        # Full prompt/response as log attributes so they are queryable in
        # Bronto, using the current semconv names/shape (the deprecated flat
        # gen_ai.prompt / gen_ai.completion / gen_ai.system_prompt attributes
        # were removed from the GenAI registry).
        "gen_ai.input.messages": input_messages,
        "gen_ai.output.messages": output_messages,
    }
    if system_instructions:
        log_attrs["gen_ai.system_instructions"] = system_instructions
    log.info("bedrock.converse", extra=log_attrs)

    return {
        "text": text,
        "model": MODEL_ID,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "latency_ms": round(latency_ms, 1),
        "stop_reason": finish_reason,
    }
