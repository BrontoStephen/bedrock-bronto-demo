# Bedrock → Bronto observability demo

A small AWS Bedrock web app that emits **logs, metrics, and traces** to
[Bronto.io](https://bronto.io) via OpenTelemetry. It runs on **ECS Fargate**
with an **ADOT (AWS Distro for OpenTelemetry) Collector sidecar** that forwards
all three signals to Bronto's OTLP endpoints.

```
Browser ──HTTP──► ALB ──► ECS Fargate task
                           ├─ app container (FastAPI + OTel SDK)  ──OTLP──► collector
                           │     └─ boto3 Bedrock Converse (Amazon Nova)
                           └─ ADOT collector sidecar  ──OTLP/HTTP + x-bronto-api-key──►
                                 https://ingestion.eu.bronto.io/v1/{logs,metrics,traces}
```

The app exports to the collector over loopback (`localhost:4318`). The collector
holds the Bronto credential and fans each signal out to the matching Bronto
endpoint, keeping the app code vendor-neutral.

The collector can broadcast to a **second Bronto account** too — an optional
`otlphttp/bronto2` exporter runs alongside the first in every pipeline. Leave
`bronto_api_key_2` / `bronto_otlp_base_2` unset (the default) to only export to
one account; pass `-var bronto_api_key_2=...` / `TF_VAR_bronto_api_key_2` (same
way as `bronto_api_key` below), or `BRONTO_API_KEY_2` / `BRONTO_OTLP_BASE_2` in
`.env` for local dev, to activate the second.

## What the app does

A single-page chat UI backed by a FastAPI service (`app/main.py`) with three
endpoints:

- `GET /` — serves the static chat page.
- `GET /healthz` — ALB health check.
- `POST /chat` — takes `{"prompt": "...", "system": "..."}` and calls the
  **Amazon Bedrock Converse API** (`app/bedrock.py`) with an EU cross-region
  inference profile (default `eu.amazon.nova-micro-v1:0`, overridable via
  `BEDROCK_MODEL_ID`). Returns the model's reply plus token counts, latency
  and stop reason.

A scheduled **driver Lambda** (`infra/lambda/driver.py`, every 10 minutes)
POSTs rotating prompts to `/chat`, so telemetry flows into Bronto around the
clock. The app exists to *generate* realistic GenAI telemetry; observability
is the product, the chat is the excuse.

## The OpenTelemetry data it generates

All three signals are emitted by the app's in-process OTel SDK
(`app/telemetry.py`), shipped over OTLP/HTTP to the ADOT sidecar, and
forwarded to Bronto. Resource attributes on everything: `service.name`
(`bedrock-bronto-demo`), `service.namespace` (`bronto-demos`),
`deployment.environment.name`, plus `telemetry.exporter=adot-collector`
stamped by the collector.

### Traces

Each chat request produces one trace with two spans:

| Span | Instrumentation | Key attributes |
|------|-----------------|----------------|
| `POST /chat` (server) | `opentelemetry-instrumentation-fastapi` (stable HTTP semconv) | `http.request.method`, `http.route`, `url.path`, `http.response.status_code`; plus the GenAI content set here by `app/bedrock.py`: `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions` (JSON, current GenAI semconv shape) |
| `Bedrock Runtime.Converse` (client, nested) | `opentelemetry-instrumentation-botocore` Bedrock extension | `gen_ai.operation.name=chat`, `gen_ai.provider.name=aws.bedrock` (renamed from deprecated `gen_ai.system` by the collector), `gen_ai.request.model`, `gen_ai.request.temperature`, `gen_ai.request.max_tokens`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `rpc.*` AWS call attributes |

### Metrics

| Metric | Type / unit | Source | Attributes |
|--------|-------------|--------|------------|
| `gen_ai.client.token.usage` | histogram, `{token}` | botocore instrumentation | `gen_ai.token.type` (input/output), `gen_ai.provider.name`, `gen_ai.operation.name`, `gen_ai.request.model` |
| `gen_ai.client.operation.duration` | histogram, `s` | botocore instrumentation | same as above |
| `http.server.request.duration` | histogram, `s` | FastAPI instrumentation (stable HTTP semconv) | `http.request.method`, `http.route`, `http.response.status_code` |
| `demo.bedrock.tokens` | counter, `{token}` | custom meter in `app/bedrock.py` | `model`, `direction` (input/output) |
| `demo.bedrock.latency` | histogram, `ms` | custom meter | `model` |
| `demo.bedrock.invocations` | counter, `{call}` | custom meter | `model`, `outcome` (success/error) |

The `demo.*` series duplicate what the semconv metrics carry, deliberately —
they give the demo simple, memorable names to chart in Bronto.

### Logs

Python `logging` is bridged to OTel via `LoggingHandler`, so every app log
record ships to Bronto trace-correlated (and still prints to stdout for
CloudWatch):

- `chat.request` — one per request; `prompt.chars`.
- `bedrock.converse` — one per Bedrock call; `gen_ai.request.model`,
  `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`,
  `gen_ai.response.finish_reasons`, `latency_ms`, `outcome`, and the full
  content as `gen_ai.input.messages` / `gen_ai.output.messages` /
  `gen_ai.system_instructions` (JSON, same shape as on the span). On failure:
  `outcome=error` + `error.message`.
- `gen_ai.user.message` / `gen_ai.system.message` / `gen_ai.choice` — emitted
  by the botocore instrumentation itself (the deprecated per-role content
  events; see the semconv section below).

## Getting the latest GenAI semantic conventions

The OTel GenAI conventions are still *experimental* and moved fast in
2025/2026: `gen_ai.system` became `gen_ai.provider.name`, and prompt/response
content moved from per-role log events (`gen_ai.user.message`, `gen_ai.choice`,
…) to JSON-encoded `gen_ai.input.messages` / `gen_ai.output.messages` /
`gen_ai.system_instructions` attributes. This repo pins the latest SDK
(`opentelemetry-sdk 1.43.0` / contrib `0.64b0`) and uses four mechanisms to
emit the *current* shape rather than the deprecated one:

1. **`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental,http`** — set in
   `docker-compose.yml` / `infra/ecs.tf` and merged (never overwritten) by
   `app/telemetry.py`. The `http` token switches the FastAPI instrumentation
   to the stable HTTP conventions (without it you get deprecated `http.method`
   / `http.server.duration`). The `gen_ai_latest_experimental` token is
   **ignored by the botocore Bedrock extension up to 0.64b0** — it always
   emits the legacy GenAI shape — but is kept so the app flips to the new
   shape automatically once upstream migrates to `opentelemetry-util-genai`.
2. **`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`** — the switch
   the Bedrock extension *does* read today; enables prompt/response content
   capture.
3. **Manual current-shape content attributes** — because of (1), the upstream
   instrumentation only emits content as deprecated log events, which never
   reach the span. `app/bedrock.py` therefore sets `gen_ai.input.messages` /
   `gen_ai.output.messages` / `gen_ai.system_instructions` (current semconv
   names and JSON schema) itself, on the server span and on the
   `bedrock.converse` log record.
4. **Collector-side rename of `gen_ai.system`** — the ADOT collector's
   `attributes/genai` processor (`collector/otel-collector-config.yaml`)
   renames the deprecated `gen_ai.system` attribute the instrumentation still
   emits to `gen_ai.provider.name` on traces, metrics and logs. It's an
   `insert`+`delete`, so it becomes a no-op once upstream emits
   `gen_ai.provider.name` natively.

## Layout

```
app/                      FastAPI app + OTel bootstrap + Bedrock wrapper
collector/                ADOT collector config (otlp receiver → otlphttp to Bronto)
docker/Dockerfile.app     App image
docker-compose.yml        Local smoke test (app + collector)
infra/                    Terraform: ECR, ECS Fargate, ALB, IAM, Secrets Manager, SSM
infra/lambda/driver.py    Scheduled Lambda that POSTs rotating prompts to /chat
```

## Prerequisites

- AWS credentials (Bedrock + ECS/IAM/VPC/ALB/ECR/Secrets permissions) in `eu-west-1`.
- Bedrock model access for the chosen model. **Note:** Anthropic Claude models are
  gated on some account types ("channel program accounts"); this demo defaults to
  **Amazon Nova** (`eu.amazon.nova-micro-v1:0`), which works broadly. Override with
  `BEDROCK_MODEL_ID` / the `bedrock_model_id` Terraform variable.
- A Bronto ingestion API key.

## Run locally

```bash
cp .env.example .env            # fill in BRONTO_API_KEY
eval "$(aws configure export-credentials --format env)"   # temp creds for boto3
docker compose up --build
# open http://localhost:8000
```

## Deploy to AWS

```bash
cd infra
terraform init
export TF_VAR_bronto_api_key='<your-bronto-key>'
eval "$(aws configure export-credentials --format env)"

# 1) create the ECR repo
terraform apply -auto-approve -target=aws_ecr_repository.app

# 2) build + push the app image (Fargate is linux/amd64)
REPO=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region eu-west-1 \
  | docker login --username AWS --password-stdin "${REPO%/*}"
docker buildx build --platform linux/amd64 -f ../docker/Dockerfile.app -t "${REPO}:latest" --push ..

# 3) deploy everything
terraform apply -auto-approve

# 4) use it
open "$(terraform output -raw alb_url)"
```

To redeploy app code: rebuild/push the image (step 2), then
`aws ecs update-service --cluster bedrock-bronto-demo --service bedrock-bronto-demo --force-new-deployment --region eu-west-1`.

## Always-on + weekly security patching

The ECS service runs `desired_count = 1` and ECS restarts the task if it ever
stops, so the demo stays continuously available at the ALB URL.

**Continuous telemetry:** a driver Lambda (`infra/lambda/driver.py`, wired up
in `infra/driver.tf`) is invoked by an EventBridge rule every 10 minutes and
POSTs a rotating prompt to the ALB's `/chat` endpoint, so traces, logs and
metrics stream into Bronto around the clock even when nobody is using the demo
by hand. Prompts vary in topic and length (and occasionally set a system
prompt) to keep the gen-AI spans and token metrics interesting. Change the
cadence via the `schedule_expression` Terraform variable.

A **weekly automated rebuild** keeps the image patched ahead of the account's
security scan:

```
EventBridge Scheduler (Mon 03:00 UTC)
  -> CodeBuild "bedrock-bronto-demo-weekly-rebuild"
       -> docker build --pull   (latest patched python:3.12-slim + apt upgrade)
       -> push :latest + :<date>-<build> to ECR
       -> ecs update-service --force-new-deployment
```

- OS patching happens in `docker/Dockerfile.app` (`apt-get upgrade`) combined
  with `docker build --pull` in `buildspec.yml`.
- Build source is a snapshot of this repo zipped to S3 by Terraform. After
  editing app code, run `terraform apply` again to refresh the snapshot.
- Run it on demand:
  `aws codebuild start-build --project-name bedrock-bronto-demo-weekly-rebuild --region eu-west-1`
- Change the cadence via `aws_scheduler_schedule.weekly_rebuild` in `infra/cicd.tf`.

## Structured logs

App logs use a short event name as the body plus structured `extra={...}`
attributes (e.g. `gen_ai.usage.input_tokens`, `latency_ms`, `outcome`). The OTel
`LoggingHandler` maps these into log-record attributes, so Bronto indexes them
as first-class, correctly-typed fields (NUMBER/STRING) rather than parsing a
flat text line. Avoid printf-style `log.info("... %s", x)` for data you want to
query — put it in `extra` instead.

Prompt/response content lands on both the log record and the span (see "The
OpenTelemetry data it generates" above) because Bronto indexes plain span
attributes for trace search, while content tucked inside log events isn't
visible from the trace view.

## Verify

- App: `curl "$(terraform output -raw alb_url)/healthz"` and POST to `/chat`.
- Collector exports: check CloudWatch log group `/ecs/bedrock-bronto-demo/collector`
  for per-signal summaries and the absence of `Exporting failed`.
- Bronto: confirm traces/metrics/logs for `service.name=bedrock-bronto-demo` arrive.

## Teardown

```bash
cd infra && terraform destroy -auto-approve
```

## Notes

- Collector config is delivered to the sidecar via the ADOT `AOT_CONFIG_CONTENT`
  env var, sourced from an SSM parameter; the Bronto key comes from Secrets Manager.
- Bronto OTLP metrics ingestion (`/v1/metrics`) is currently a closed beta.
- The collector logs a harmless deprecation warning: `"otlphttp" alias is
  deprecated; use "otlp_http"`. Either key works on the current ADOT image.
