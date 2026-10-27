# AvionPulse — Datadog Setup

This guide takes you from a plain AvionPulse deployment to a fully monitored one:

1. [Lambda instrumentation](#1-lambda-instrumentation) — traces, logs, enhanced and custom metrics
2. [AWS integration](#2-aws-integration) — API Gateway, DynamoDB, SQS and CloudFront metrics
3. [RUM](#3-rum-optional) (optional)
4. [Reference: names, tags, metrics and log lines](#4-reference-names-tags-metrics-log-lines)
5. [Creating monitors — the common steps](#5-creating-monitors--the-common-steps)
6. [One monitor per fault](#6-one-monitor-per-fault) — all 23 chaos faults (incl. the composite), with test steps
7. [The AvionPulse Health dashboard](#7-the-avionpulse-health-dashboard) — build by hand or import JSON
8. [Verifying everything end to end](#8-verifying-everything-end-to-end)

AvionPulse runs without Datadog. Everything below assumes you deployed **with** a Datadog API key.

---

## 1. Lambda instrumentation

```powershell
./scripts/deploy.ps1 -DatadogApiKey <DD_API_KEY> -DatadogSite datadoghq.com
```
```bash
DD_API_KEY=<key> DD_SITE=datadoghq.com ./scripts/deploy.sh
```

What the script and template do:

| Piece | How |
|---|---|
| API key | Stored as SSM **SecureString** `/avionpulse/datadog/api-key` (free); the extension reads it via `DD_API_KEY_SSM_NAME`. The key is never in the template or env vars. |
| Layers | `Datadog-Python312-ARM` + `Datadog-Extension-ARM` from account `464622532012`. |
| Handler | Switched to `datadog_lambda.handler.handler`; the real handler is in `DD_LAMBDA_HANDLER`. |
| Tags | `DD_SERVICE=avionpulse`, `DD_ENV`, `DD_VERSION`, `DD_TAGS=app:avionpulse,suite:aviondash,stack:<stack>` |
| Traces | `DD_TRACE_ENABLED`, `DD_COLD_START_TRACING`; botocore is auto-instrumented, so every DynamoDB / SQS / S3 / Lambda / API Gateway call is its own span. |
| Logs | `DD_SERVERLESS_LOGS_ENABLED` — the extension ships logs directly (no Forwarder Lambda). Lambda logs are JSON (`LoggingConfig: JSON`), with trace IDs injected. |
| Enhanced metrics | `DD_ENHANCED_METRICS` — near-real-time `aws.lambda.enhanced.*` (timeouts, OOM, init duration, cold starts, cost). |
| Custom metrics | `datadog_lambda.metric.lambda_metric` through the extension — no CloudWatch custom-metric charges. |

**Layer versions.** The defaults (`107` / `80`) are placeholders. Check the current versions
in Datadog's *Serverless → AWS Lambda → Python* install docs, then pass
`-DatadogPythonLayerVersion` / `-DatadogExtensionLayerVersion` (or `DD_PY_LAYER` / `DD_EXT_LAYER`).
A version that doesn't exist fails the deploy with a "layer version not found" error.

**Check it worked.** Open **Infrastructure → Serverless**. Within a few minutes of browsing the site
you should see four functions: `avionpulse-api`, `avionpulse-worker`, `avionpulse-simulator`,
`avionpulse-seed`. Click `avionpulse-api` → the *Traces* tab should show invocations with
DynamoDB child spans.

## 2. AWS integration

Lambda metrics come from the extension; API Gateway, DynamoDB, SQS, CloudFront and the
integration-level Lambda metrics (`aws.lambda.throttles`) need the Datadog AWS integration.

1. **Integrations → Amazon Web Services → Add AWS Account → Automatically using CloudFormation.**
   Pick the region you deployed to and launch the quick-create stack in the AWS console.
2. When the account shows as connected, open it and go to **Metric Collection**:
   * Untick every namespace except **Lambda, ApiGateway, DynamoDB, SQS, CloudFront, Events**.
   * Under *Limit metric collection by tag*, add `app:avionpulse` (the template tags every
     resource with it).
   * This keeps the CloudWatch `GetMetricData` bill (paid to AWS, $0.01 per 1,000 metrics) to cents.
3. **Log Collection** — leave off. The Lambda Extension already ships Lambda logs; the API Gateway
   access-log group is optional.
4. CloudFront metrics only exist in **us-east-1** regardless of where you deployed.

> **Latency of integration metrics.** `aws.apigateway.*`, `aws.dynamodb.*`, `aws.sqs.*` and
> `aws.lambda.throttles` arrive by polling CloudWatch, typically **5–15 minutes late**.
> `aws.lambda.enhanced.*`, custom `avionpulse.*` metrics, traces and logs arrive within about a minute.
> The monitor windows below are sized for that.

## 3. RUM (optional)

1. **Digital Experience → Add an Application → JS**, name it `avionpulse-web`.
2. Copy the *Application ID* and *Client Token*, then redeploy:

```powershell
./scripts/deploy.ps1 -DatadogApiKey <key> -RumApplicationId <app-id> -RumClientToken <pub-token>
```

`config.js` is generated at deploy time. RUM uses `allowedTracingUrls: [<site>/api]`, so browser
sessions link to the Lambda traces (CloudFront forwards the `x-datadog-*` headers to API Gateway).

---

## 4. Reference: names, tags, metrics, log lines

### Resource names

All names derive from the stack name. With the default stack name `avionpulse`:

| Resource | Name / tag value | Used in queries as |
|---|---|---|
| API Lambda | `avionpulse-api` | `functionname:avionpulse-api` |
| Worker Lambda | `avionpulse-worker` | `functionname:avionpulse-worker` |
| Simulator Lambda | `avionpulse-simulator` | `functionname:avionpulse-simulator` |
| DynamoDB table | `avionpulse-data` | `tablename:avionpulse-data` |
| Events queue | `avionpulse-events` | `queuename:avionpulse-events` |
| Dead-letter queue | `avionpulse-events-dlq` | `queuename:avionpulse-events-dlq` |
| HTTP API | stack output `ApiId` | `apiid:<ApiId>` |
| Everything | resource tag | `app:avionpulse` |
| Service / env | from `DD_SERVICE` / `DD_ENV` | `service:avionpulse`, `env:demo` |

Get the API id with:

```bash
aws cloudformation describe-stacks --stack-name avionpulse --query "Stacks[0].Outputs[?OutputKey=='ApiId'].OutputValue" --output text
```

**Using a different stack name or env?** Replace `avionpulse-` and `env:demo` everywhere below.

### Custom metrics

`lambda_metric` submits everything as Datadog **distribution** metrics. Query gauges with `avg:`
and counts with `sum:…as_count()`. Every custom metric also carries `env:<AppEnv>`. Without the
layer, each metric is written to the logs as a JSON line (`metric_name`, `metric_value`,
`metric_tags`) instead.

| Metric | Read as | Tags | Emitted by |
|---|---|---|---|
| `avionpulse.flights.airborne` | gauge | env | simulator (every 5 min) |
| `avionpulse.flights.delayed` / `.cancelled` / `.total` | gauge | env | simulator |
| `avionpulse.flights.on_time_pct` | gauge | env | simulator |
| `avionpulse.pax.airborne` | gauge | env | simulator |
| `avionpulse.fleet.in_maintenance` | gauge | env | simulator |
| `avionpulse.api.requests` | count | route, method, status_class, cold_start | api |
| `avionpulse.api.latency_ms` | distribution | route, method, cold_start | api |
| `avionpulse.api.flights_returned` | gauge | env | api |
| `avionpulse.data.staleness_s` | gauge | env | api (dashboard summary) |
| `avionpulse.auth.login` | count | result, role | api |
| `avionpulse.events.submitted` / `.processed` / `.poison` | count | type, severity | api / worker |
| `avionpulse.queue.processing_lag_s` | distribution | type | worker |
| `avionpulse.chaos.toggle` | count | fault, enabled | api |

**Enable percentiles** for `avionpulse.api.latency_ms`: *Metrics → Summary →
avionpulse.api.latency_ms → Advanced → Enable percentiles*. Needed for the p95 queries below.

### Log lines the monitors key on

These strings come straight from the code, so log monitors can match them exactly.

| Log text (search term) | Where | Meaning |
|---|---|---|
| `"Request failed"` | api | Any handled 5xx. Format: `Request failed GET /api/notams -> 502 NoSuchKey: …` |
| `ProvisionedThroughputExceededException` | api | DynamoDB throttled |
| `AccessDeniedException` / `"is not authorized to perform"` | api | IAM denial |
| `NoSuchKey` | api | S3 object missing |
| `UpstreamTimeout` | api | Third-party call timed out |
| `"unhandled exception escaping handler"` | api | Raised on purpose; also logged by the Lambda runtime as an `[ERROR]` |
| `"Task timed out"` | Lambda platform | Function hit its 10 s timeout |
| `"Poison message"` | worker | SQS message failed validation |
| `SimulatorCrash` | Lambda runtime | Scheduled simulator raised |
| `"Failed login for"` | api | Bad password |
| `[CHAOS]` | api, simulator | Fault switched on/off/expired (handy for event overlays) |

### Span-based DynamoDB call metric (used by two monitors)

There's no CloudWatch metric that counts DynamoDB calls by operation per request, so generate one
from APM spans:

1. **APM → Setup & Configuration → Generate Metrics → New Metric**.
2. Open any `avionpulse-api` trace first and click a DynamoDB child span. Note the attribute that
   holds the operation (normally `aws.operation`, value `Query`, `Scan`, `GetItem`…) and the
   service shown for the span.
3. Fill in:
   * **Metric name:** `avionpulse.ddb.calls`
   * **Query:** `env:demo @aws.operation:*` and, if you want to be strict, the DynamoDB span
     service you noted (e.g. `service:aws.dynamodb`).
   * **Group by:** `@aws.operation`
   * **Aggregation:** count
4. Save. The metric fills in from new spans only (not retroactively).

---

## 5. Creating monitors — the common steps

Every monitor in section 6 follows these steps; only the query, thresholds and message change.

1. **Monitors → New Monitor**, then choose the type listed for the fault (*Metric*, *Logs*,
   *Anomaly*, *Forecast*, *Composite* or *Synthetic*).
2. **Define the metric / search.** Click **Edit** (the `</>` source toggle) next to the query and
   paste the query from section 6. Datadog fills in the form for you. For log monitors, paste the
   search into the search bar and set *count of logs*.
3. **Set alert conditions.** Enter the *Alert* and *Warning* thresholds given. Leave
   *Require a full window of data* **off** for integration metrics (they arrive late and patchy).
4. **Missing data.** Default is *Do not notify* unless the fault says otherwise.
5. **Notify your team.**
   * **Name:** use the name given, e.g. `[AvionPulse] DynamoDB read throttling`.
   * **Message:** start from the template below, then paste the fault-specific runbook line.
   * **Recipients:** `@your-email` or `@slack-channel`.
   * **Renotify:** every 30 min while alerting (optional).
6. **Tags:** add `app:avionpulse`, `service:avionpulse`, `env:demo`, and `fault:<fault_name>`.
   The dashboard's monitor-summary widget filters on `app:avionpulse`, and `fault:` lets you see
   which chaos switch each monitor proves.
7. **Priority:** P1 for *critical* faults, P3 for *warning* faults (matches the chaos page badges).
8. **Create.**

**Message template**

```text
{{#is_alert}}
AvionPulse: {{monitor_name}} is ALERTING ({{value}}).
{{/is_alert}}
{{#is_warning}}
AvionPulse: {{monitor_name}} is in WARNING ({{value}}).
{{/is_warning}}
{{#is_recovery}}
AvionPulse: {{monitor_name}} recovered.
{{/is_recovery}}

Runbook: <fault-specific line from section 6>
Chaos panel: https://<your-cloudfront-domain>/#chaos (check for an active fault first)
Dashboard: AvionPulse — Serverless Health

@your-email
```

---

## 6. One monitor per fault

Each entry gives the monitor that detects the fault, the exact settings, and how to prove it
fires. **Test procedure for all of them:** sign in as `admin`, open **Chaos Control**, pick a TTL
of 15–30 minutes, switch the fault on, generate traffic (browse the pages, or run
`python scripts/loadgen.py https://<site> --rps 1 --minutes 10`), wait the time stated, confirm
the monitor alerts, then switch the fault off and confirm it recovers.

> Times to alert assume the windows below. Enhanced, custom and log signals take about 1–3 minutes;
> CloudWatch-polled signals take about 10–20 minutes.

### 6.1 Edge tier

#### `api_throttling` — API Gateway throttling
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] API Gateway throttling (429s)`
* **Query:**
  ```
  sum(last_10m):sum:aws.apigateway.4xx{apiid:<ApiId>}.as_count() > 20
  ```
* **Thresholds:** Alert `> 20`, Warning `> 5`
* **Make it smarter (optional composite):** create a second monitor
  `[AvionPulse] API Lambda invocations dropped`,
  `pct_change(avg(last_10m),last_1h):sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api}.as_count() < -50`,
  then a **Composite** monitor `A && B`. "Users see errors, but Lambda runs *less*" points straight
  at the edge, not the code.
* **Runbook:** Check the HTTP API `$default` stage throttle. Template default is 20 rps / burst 40.
  Clear the fault or run RESET ALL.
* **Test:** fault on, run loadgen at `--rps 3`. Alerts in about 10–20 min (CloudWatch delay).

#### `latency_spike` — Latency spike
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] API latency p95 high`
* **Query:**
  ```
  avg(last_5m):p95:avionpulse.api.latency_ms{env:demo} > 2000
  ```
  (Needs percentiles enabled — section 4. Without them use
  `avg(last_5m):avg:aws.lambda.enhanced.duration{functionname:avionpulse-api} > 2`, in seconds.)
* **Thresholds:** Alert `> 2000` ms, Warning `> 1000` ms
* **Runbook:** Compare `aws.apigateway.integration_latency` with Lambda duration. If both are high,
  the time is in the function; open a slow trace and look for the long gap before the first span.
* **Test:** fault on, browse. Alerts in about 2–5 min.

#### `random_500s` — Unhandled exceptions
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] API Lambda error rate`
* **Query:**
  ```
  sum(last_10m):sum:aws.lambda.enhanced.errors{functionname:avionpulse-api}.as_count() / sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api}.as_count() * 100 > 10
  ```
* **Thresholds:** Alert `> 10` (%), Warning `> 3`
* **Also useful:** **APM → Error Tracking** groups these as one issue
  (`UnhandledFault: FlightBoardRenderError…`). Turn on *Notify on new issues* for `service:avionpulse`.
* **Runbook:** Error Tracking → open the newest issue → the stack trace names the route.
* **Test:** fault on, refresh the Flights page about 20 times. Alerts in about 2–5 min.

#### `health_check_fail` — Health check failure
* **Type:** Synthetic Monitoring → **API test** (HTTP)
* **Steps:** *Digital Experience → Synthetic Monitoring → New Test → API Test → HTTP*.
  * **URL:** `GET https://<site>/api/health`
  * **Assertions:** `status code is 200`; `body contains "ok"`; `response time is less than 3000 ms`
  * **Locations:** 1–2 managed locations near your region (each extra location adds runs)
  * **Frequency:** every 5 minutes
  * **Alert:** *alert if test fails for 2 minutes from 1 of N locations*, retry once after 30 s
  * **Name:** `[AvionPulse] Health endpoint`, tags as in section 5
* **Runbook:** `/api/health` only checks the function; call `/api/health/deep` to see DynamoDB,
  SQS and S3 individually.
* **Test:** fault on. Alerts on the next run (≤ 5 min).

### 6.2 Compute tier

#### `lambda_timeout` — Lambda timeout
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Lambda timeouts`
* **Query:**
  ```
  sum(last_5m):sum:aws.lambda.enhanced.timeouts{service:avionpulse}.as_count() > 0
  ```
* **Thresholds:** Alert `> 0`
* **Multi-alert:** under *Group by* choose `functionname` so the alert names the function.
* **Runbook:** Logs → `service:avionpulse "Task timed out"`. Duration graph will be pinned at 10 s.
* **Test:** fault on, refresh Flights about 10 times (30% of requests time out). Alerts in about 2 min.

#### `lambda_oom` — Out of memory
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Lambda out of memory`
* **Query:**
  ```
  sum(last_5m):sum:aws.lambda.enhanced.out_of_memory{service:avionpulse}.as_count() > 0
  ```
* **Thresholds:** Alert `> 0`; group by `functionname`
* **Runbook:** Check `aws.lambda.enhanced.max_memory_used` against the 256 MB size. One-off spike
  means a single bad request; a steady climb means a leak (see next).
* **Test:** fault on, refresh Flights about 10 times (25% OOM). Alerts in about 2 min.

#### `memory_leak` — Warm-container memory leak
* **Type:** Forecast
* **Name:** `[AvionPulse] API memory forecast to hit limit`
* **Query:**
  ```
  max(next_1h):forecast(max:aws.lambda.enhanced.max_memory_used{functionname:avionpulse-api}, 'linear', 1) >= 250
  ```
* **Thresholds:** Alert `>= 250` (MB; memory size is 256), Warning `>= 200`
* **Simpler alternative (Metric → Threshold):**
  `max(last_10m):max:aws.lambda.enhanced.max_memory_used{functionname:avionpulse-api} > 180`
* **Runbook:** A sawtooth (climb, OOM, drop) is the signature of state kept between invocations.
  Look for module-level lists or caches.
* **Test:** fault on, run loadgen at `--rps 1` for 10 min. Warning in about 5 min, OOMs follow.

#### `cpu_burn` — CPU burn
* **Type:** Anomaly
* **Name:** `[AvionPulse] API billed duration anomaly`
* **Query:**
  ```
  avg(last_15m):anomalies(avg:aws.lambda.enhanced.billed_duration{functionname:avionpulse-api}, 'agile', 2, direction='above', alert_window='last_5m', interval=60, count_default_zero='true') >= 1
  ```
* **Thresholds:** Alert `>= 1` (100% of the alert window anomalous), Recovery `<= 0`
* **Needs history:** an anomaly monitor needs a few days of normal traffic to learn from. Until
  then use `avg(last_10m):avg:aws.lambda.enhanced.billed_duration{functionname:avionpulse-api} > 0.35`.
* **Runbook:** Check `aws.lambda.enhanced.estimated_cost`. Duration up without more DynamoDB
  time in traces means the code is burning CPU.
* **Test:** fault on, browse for 5 min. Threshold version alerts in about 3 min.

#### `cold_start_storm` — Cold start storm
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Cold start storm`
* **Query:**
  ```
  sum(last_10m):sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api,cold_start:true}.as_count() > 10
  ```
* **Thresholds:** Alert `> 10`, Warning `> 5`
* **Companion monitor (init time):**
  `avg(last_15m):avg:aws.lambda.enhanced.init_duration{functionname:avionpulse-api} > 2`
  (seconds; the fault adds 2.5 s to init).
* **Runbook:** Serverless view → `avionpulse-api` → *Cold starts*. A spike right after a
  configuration change means a deploy or config update recycled the containers.
* **Test:** fault on (it recycles containers immediately and again every simulator run), open
  several pages in a few browser tabs. Alerts in about 2–5 min.

#### `worker_throttle` — Worker concurrency = 0
* **Type:** Metric → Threshold (two monitors, use both)
* **Name 1:** `[AvionPulse] Worker throttled`
  ```
  sum(last_15m):sum:aws.lambda.throttles{functionname:avionpulse-worker}.as_count() > 0
  ```
* **Name 2:** `[AvionPulse] Events queue backing up`
  ```
  max(last_15m):max:aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events} > 20
  ```
  Warning `> 10`
* **Why two:** depending on how Lambda's SQS poller backs off, reserved concurrency 0 doesn't
  always produce many throttle datapoints. Queue depth always shows it.
* **Runbook:** `aws lambda get-function-concurrency --function-name avionpulse-worker`. A value of
  0 means someone throttled it. RESET ALL removes the reservation.
* **Test:** fault on; raise 5 maintenance requests on **Ops Pipeline** (as admin or operator).
  Queue monitor alerts in about 15–25 min.

### 6.3 Data tier

#### `dynamodb_throttle` — DynamoDB read storm
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] DynamoDB read throttling`
* **Query:**
  ```
  sum(last_10m):sum:aws.dynamodb.read_throttle_events{tablename:avionpulse-data}.as_count() > 0
  ```
* **Fast companion (Logs):** `[AvionPulse] DynamoDB throttled (logs)`
  * Search: `service:avionpulse ProvisionedThroughputExceededException`
  * Condition: count `> 5` over last 5 minutes
  This alerts in about a minute, before the CloudWatch metric lands.
* **Runbook:** Compare `aws.dynamodb.consumed_read_capacity_units` with
  `aws.dynamodb.provisioned_read_capacity_units`. Look for a Scan-heavy trace.
* **Test:** fault on, refresh Flights repeatedly for about 1 min. Burst credit drains, then 503s
  begin. Log monitor about 2 min; metric monitor about 10–20 min.

#### `full_table_scan` — Scan instead of Query
* **Type:** Metric → Threshold (uses the span-based metric from section 4)
* **Name:** `[AvionPulse] DynamoDB Scan in request path`
* **Query:**
  ```
  sum(last_10m):sum:avionpulse.ddb.calls{@aws.operation:scan}.as_count() > 5
  ```
  (Tag values in generated metrics are lowercased. Check the exact tag in Metrics Explorer.)
* **Without the span metric:** threshold on consumed capacity,
  `sum(last_15m):sum:aws.dynamodb.consumed_read_capacity_units{tablename:avionpulse-data}.as_count() > 3000`.
  Tune it to about 3× your normal level after a day of traffic.
* **Runbook:** Open a recent `GET /api/flights` trace. The DynamoDB child span should say
  `Query`, not `Scan`.
* **Test:** fault on, refresh Flights 10 times. Span metric monitor alerts in about 3 min.

#### `n_plus_one` — N+1 GetItem calls
* **Type:** Metric → Threshold (span-based metric)
* **Name:** `[AvionPulse] DynamoDB N+1 pattern`
* **Query:**
  ```
  sum(last_10m):sum:avionpulse.ddb.calls{@aws.operation:getitem}.as_count() / sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api}.as_count() > 10
  ```
* **Thresholds:** Alert `> 10` GetItem calls per invocation (normal is about 0–1; the fault does about 50)
* **Runbook:** Trace flame graph → dozens of identical `GetItem` spans under one request. Replace
  them with one Query or BatchGetItem.
* **Test:** fault on, refresh Flights 5 times. Alerts in about 3 min.

#### `iam_access_denied` — IAM permission denied
* **Type:** Logs
* **Name:** `[AvionPulse] IAM AccessDenied`
* **Search:**
  ```
  service:avionpulse (AccessDeniedException OR "is not authorized to perform")
  ```
* **Condition:** count `> 0` over the last 5 minutes. Group by `@functionname` if present.
* **Message extra:** add `{{log.message}}` to the body. The log line contains the exact role and
  resource ARN.
* **Runbook:** The message names the action and the resource. Compare with the Lambda role. For
  AvionPulse the missing grant is on `<stack>-data-restricted-ledger` (deliberate).
* **Test:** fault on, open the Dashboard (the summary call fails with 500). Alerts in about 1–2 min.

#### `s3_missing_object` — S3 object missing
* **Type:** Logs
* **Name:** `[AvionPulse] S3 NoSuchKey`
* **Search:**
  ```
  service:avionpulse NoSuchKey
  ```
* **Condition:** count `> 0` over the last 5 minutes
* **Runbook:** The error names the key. `aws s3 ls s3://<bucket>/data/` to confirm what was uploaded;
  redeploying runs `s3 sync` again.
* **Test:** fault on, open **NOTAMs**. Alerts in about 1–2 min.

### 6.4 Async tier

#### `sqs_poison_pill` — Poison messages
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Messages in dead-letter queue`
* **Query:**
  ```
  max(last_15m):max:aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events-dlq} > 0
  ```
* **Thresholds:** Alert `> 0`, Warning: none
* **Fast companion (Metric):** `sum(last_5m):sum:avionpulse.events.poison{env:demo}.as_count() > 3`
  catches the worker rejecting messages before they reach the DLQ.
* **Runbook:** Logs → `service:avionpulse "Poison message"`. Inspect a DLQ message with
  `aws sqs receive-message --queue-url <DeadLetterQueueUrl>`. After a fix, redrive it from the
  SQS console (*Start DLQ redrive*).
* **Recovery note:** the DLQ doesn't empty itself. Purge it in the SQS console after testing, or the
  monitor stays red for 4 days (retention).
* **Test:** fault on, browse for 2 min. Poison counter about 2 min; DLQ monitor about 15–25 min
  (3 receives × 60 s visibility, plus CloudWatch delay).

#### `queue_backlog` — Worker trigger paused
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Events queue age`
* **Query:**
  ```
  max(last_15m):max:aws.sqs.approximate_age_of_oldest_message{queuename:avionpulse-events} > 600
  ```
* **Thresholds:** Alert `> 600` s, Warning `> 300` s
* **Runbook:** `aws lambda list-event-source-mappings --function-name avionpulse-worker` and check
  `State`. `Disabled` means the trigger is paused. RESET ALL re-enables it.
* **Test:** fault on. The simulator adds about 20 messages every 5 min. Alerts in about 20–25 min.

#### `simulator_crash` — Scheduled job crash
Create **two** monitors. The second one is the interesting one.

* **Monitor 1 — Type:** Metric → Threshold. **Name:** `[AvionPulse] Simulator errors`
  ```
  sum(last_15m):sum:aws.lambda.enhanced.errors{functionname:avionpulse-simulator}.as_count() > 0
  ```
* **Monitor 2 — Type:** Metric → Threshold. **Name:** `[AvionPulse] Flight data is stale`
  ```
  max(last_15m):max:avionpulse.data.staleness_s{env:demo} > 900
  ```
  Warning `> 600`. In **Missing data**, also tick *Notify if data is missing for more than 20 minutes*
  on a third quick monitor over `avionpulse.flights.airborne`. When the simulator crashes, the
  business metrics simply stop.
* **Runbook:** Serverless → `avionpulse-simulator` → *Errors* tab. Error Tracking will group the
  `SimulatorCrash` issue.
* **Test:** fault on. Errors alert after the next 5-minute tick; staleness alerts after about 15 min
  (only while someone is viewing the Dashboard — the API emits staleness when the summary loads).

#### `downstream_timeout` — Weather API timeout
* **Type:** Logs
* **Name:** `[AvionPulse] Third-party dependency timing out`
* **Search:**
  ```
  service:avionpulse UpstreamTimeout
  ```
* **Condition:** count `> 2` over the last 5 minutes
* **APM alternative:** **Monitors → New → APM** → *APM Metrics* on service `avionpulse`, resource
  `GET /api/weather` (if resources are named by route in your traces), Error rate `> 50%`.
* **Runbook:** The trace shows an HTTP span to `10.255.255.1` lasting 3 s. In a real system: check
  the provider's status page and whether a circuit breaker should open.
* **Test:** fault on, open the Dashboard 3 times (the weather panel calls the provider). Alerts in about 2 min.

### 6.5 Business logic

#### `silent_failure` — Silent partition-key bug
This is the showcase fault: no error, no latency change, HTTP 200 everywhere.

* **Monitor 1 — Type:** Metric → Threshold. **Name:** `[AvionPulse] No airborne flights`
  ```
  max(last_15m):avg:avionpulse.flights.airborne{env:demo} < 1
  ```
  Alert `< 1`. Normal is about 10–17 airborne.
* **Monitor 2 — Type:** Metric → Threshold. **Name:** `[AvionPulse] Flights API returning empty`
  ```
  max(last_10m):max:avionpulse.api.flights_returned{env:demo} < 1
  ```
* **Monitor 3 — Type:** Synthetic API test. **Name:** `[AvionPulse] Public stats content`
  * `GET https://<site>/api/dashboard/public-stats`
  * Assertions: status `200` **and** JSON path `$.flights.total` *greater than* `0`
  * Every 5 min, 1 location
* **Runbook:** APM shows 0% errors; that's the point. Check what changed recently (deploys) and
  query the table directly: `aws dynamodb query --table-name avionpulse-data --key-condition-expression "PK = :p" --expression-attribute-values '{":p":{"S":"FLIGHT"}}' --select COUNT`.
* **Test:** fault on. Synthetic about 5 min; airborne monitor after the next simulator run (≤ 5 min).

#### `pax_corruption` — Passenger count corruption
* **Type:** Metric → Threshold (deterministic). Use an Anomaly monitor once you have history.
* **Name:** `[AvionPulse] Implausible passenger count`
* **Query:**
  ```
  max(last_15m):max:avionpulse.pax.airborne{env:demo} > 20000
  ```
  Even if all 25 flights were airborne and full, the maximum is 6,338 seats (normal is about
  2,500–4,000), so anything over 20,000 is impossible. Under the fault it is in the millions.
* **Anomaly version:**
  `avg(last_4h):anomalies(avg:avionpulse.pax.airborne{env:demo}, 'basic', 3, direction='above', alert_window='last_15m') >= 1`
* **Runbook:** Infrastructure is fine; this is a data-quality bug. Compare a flight's `pax` with
  its aircraft `capacity` on the Flights and Fleet pages.
* **Test:** fault on. Alerts after the next simulator run (≤ 5 min).

#### `log_flood` — Log flood
* **Type:** Logs (or Anomaly on log count)
* **Name:** `[AvionPulse] Log volume spike`
* **Search:**
  ```
  service:avionpulse status:warn
  ```
* **Condition:** count `> 1000` over the last 5 minutes (normal is well under 50)
* **Also:** **Logs → Configuration → Indexes** → add a daily quota or an exclusion filter for
  `"[FAULT][LOG_FLOOD]"` if you want to protect your Datadog log budget during demos.
* **Runbook:** Logs → Patterns view. One pattern with thousands of hits is the culprit.
* **Test:** fault on, browse about 20 pages. Alerts in about 2 min.

### 6.6 Composite

#### `cascading_failure` — Cascading failure
* **Type:** **Composite**
* **Prerequisite monitors** (from above): `API latency p95 high` (A), `DynamoDB throttled (logs)` (B),
  `API Lambda error rate` (C), `Events queue age` (D), `Log volume spike` (E)
* **Name:** `[AvionPulse] P1 — cascading failure`
* **Trigger condition:**
  ```
  (a && c) && (b || d || e)
  ```
  (letters are assigned when you pick the monitors in the composite form)
* **Priority:** P1. **Message:** say *"Multiple tiers failing — start with the data tier,
  then work outwards"*.
* **Why composite:** each underlying monitor still notifies on its own; set them to *no
  notification* (remove recipients) if you want only the composite to page.
* **Test:** switch on `cascading_failure` (it turns on all five members). A and C fire in minutes,
  then B/E; the composite fires once A, C and one of the others are red.

### 6.7 Summary table

| Fault | Monitor | Type | Typical time to alert |
|---|---|---|---|
| api_throttling | API Gateway throttling (429s) | Metric (+composite) | 10–20 min |
| latency_spike | API latency p95 high | Metric | 2–5 min |
| random_500s | API Lambda error rate | Metric | 2–5 min |
| health_check_fail | Health endpoint | Synthetic | ≤ 5 min |
| lambda_timeout | Lambda timeouts | Metric | ~2 min |
| lambda_oom | Lambda out of memory | Metric | ~2 min |
| memory_leak | API memory forecast to hit limit | Forecast | ~5 min |
| cpu_burn | API billed duration anomaly | Anomaly / Metric | ~3 min |
| cold_start_storm | Cold start storm | Metric | 2–5 min |
| worker_throttle | Worker throttled + Events queue backing up | Metric ×2 | 15–25 min |
| dynamodb_throttle | DynamoDB read throttling (+ logs) | Metric + Logs | 2 / 15 min |
| full_table_scan | DynamoDB Scan in request path | Span metric | ~3 min |
| n_plus_one | DynamoDB N+1 pattern | Span metric | ~3 min |
| iam_access_denied | IAM AccessDenied | Logs | 1–2 min |
| s3_missing_object | S3 NoSuchKey | Logs | 1–2 min |
| sqs_poison_pill | Messages in DLQ (+ poison counter) | Metric | 2 / 20 min |
| queue_backlog | Events queue age | Metric | 20–25 min |
| simulator_crash | Simulator errors + Flight data is stale | Metric ×2 | 5 / 15 min |
| downstream_timeout | Third-party dependency timing out | Logs | ~2 min |
| silent_failure | No airborne flights + empty API + Synthetic | Metric ×2 + Synthetic | ≤ 5 min |
| pax_corruption | Implausible passenger count | Metric | ≤ 5 min |
| log_flood | Log volume spike | Logs | ~2 min |
| cascading_failure | P1 — cascading failure | Composite | 5–15 min |

---

## 7. The AvionPulse Health dashboard

One screen that answers "is AvionPulse healthy, and if not, which tier?". It's laid out top to
bottom in the same order as the architecture: business → edge → compute → data → async → logs.

### Option A — import the JSON (2 minutes)

1. Open [`docs/datadog/avionpulse-dashboard.json`](datadog/avionpulse-dashboard.json) and replace
   all 5 occurrences of `REPLACE_WITH_API_ID` with your `ApiId` stack output (find-and-replace;
   they're in the three API Gateway widgets).
2. **Dashboards → New Dashboard →** give it any name → **Create**.
3. Click the **gear icon (⚙) → Import dashboard JSON**, paste the file, **Yes, replace**.
4. Set the `env` template variable at the top to `demo`.

### Option B — build it by hand

**Create the dashboard**

1. **Dashboards → New Dashboard**, name `AvionPulse — Serverless Health`, layout **Dashboard**
   (the grid layout, not a Screenboard).
2. **Template variable:** click **Add Variable** → name `env`, tag/attribute `env`, default `demo`.
   Custom `avionpulse.*` queries below use `{$env}`. AWS integration metrics use resource
   names instead, because they don't all carry `env`.
3. For each group below: **Add Widgets → Group**, give it the title, then drag the listed widgets
   *into* the group. For every widget: paste the query in the **</>** (source) editor, set the
   title, then **Save**.

**Group 1 — Business health** (what the airline cares about)

| Widget | Type | Query | Settings |
|---|---|---|---|
| Airborne now | Query Value | `avg:avionpulse.flights.airborne{$env}` | Aggregation *last*; conditional format: `< 1` red, `>= 1` green |
| Passengers airborne | Query Value | `avg:avionpulse.pax.airborne{$env}` | *last*; `> 20000` red (corruption) |
| On-time % | Query Value | `avg:avionpulse.flights.on_time_pct{$env}` | *last*; unit `%`; `< 60` yellow, `>= 60` green |
| Data age | Query Value | `max:avionpulse.data.staleness_s{$env}` | *last*; unit `s`; `> 900` red, `> 600` yellow, else green |
| Flights by state | Timeseries (bars) | `avg:avionpulse.flights.airborne{$env}`, `avg:avionpulse.flights.delayed{$env}`, `avg:avionpulse.flights.cancelled{$env}` | Three queries on one widget; aliases Airborne / Delayed / Cancelled |
| Chaos switches | Timeseries (bars) | `sum:avionpulse.chaos.toggle{$env} by {fault}.as_count()` | Shows when someone flipped a fault — great for correlating everything below |

**Group 2 — Edge (CloudFront / API Gateway)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| API requests | Timeseries | `sum:aws.apigateway.count{apiid:<ApiId>}.as_count()` | Bars |
| 4xx vs 5xx | Timeseries | `sum:aws.apigateway.4xx{apiid:<ApiId>}.as_count()`, `sum:aws.apigateway.5xx{apiid:<ApiId>}.as_count()` | Bars; colour 4xx orange, 5xx red |
| Latency vs integration latency | Timeseries | `avg:aws.apigateway.latency{apiid:<ApiId>}`, `avg:aws.apigateway.integration_latency{apiid:<ApiId>}` | Lines, unit ms. Gap between them = time spent in API Gateway itself |
| App requests by status | Timeseries | `sum:avionpulse.api.requests{$env} by {status_class}.as_count()` | Stacked bars. Real time, unlike the CloudWatch widgets above |
| API latency p95 | Timeseries | `p95:avionpulse.api.latency_ms{$env}` | Line; add a marker `y = 2000` labelled "SLO" |
| CloudFront requests (optional) | Timeseries | `sum:aws.cloudfront.requests{app:avionpulse}.as_count()` | Only if the integration collects us-east-1 |

**Group 3 — Compute (Lambda)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| Invocations by function | Timeseries | `sum:aws.lambda.enhanced.invocations{service:avionpulse} by {functionname}.as_count()` | Stacked bars |
| Errors by function | Timeseries | `sum:aws.lambda.enhanced.errors{service:avionpulse} by {functionname}.as_count()` | Bars, red palette |
| Timeouts & OOMs | Timeseries | `sum:aws.lambda.enhanced.timeouts{service:avionpulse}.as_count()`, `sum:aws.lambda.enhanced.out_of_memory{service:avionpulse}.as_count()` | Bars |
| Duration (avg / max) | Timeseries | `avg:aws.lambda.enhanced.duration{functionname:avionpulse-api}`, `max:aws.lambda.enhanced.duration{functionname:avionpulse-api}` | Lines, unit s; marker `y = 10` "timeout" |
| Cold starts | Timeseries | `sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api,cold_start:true}.as_count()` | Bars, blue |
| Init duration | Timeseries | `avg:aws.lambda.enhanced.init_duration{functionname:avionpulse-api}` | Line, unit s |
| Max memory used | Timeseries | `max:aws.lambda.enhanced.max_memory_used{service:avionpulse} by {functionname}` | Lines, unit MB; marker `y = 256` "limit". Leak shows as a sawtooth |
| Throttles | Timeseries | `sum:aws.lambda.throttles{app:avionpulse} by {functionname}.as_count()` | Bars |
| Estimated Lambda cost | Query Value | `sum:aws.lambda.enhanced.estimated_cost{service:avionpulse}.as_count()` | Time frame *past 1 month* (pin it in widget settings); unit `$` |

**Group 4 — Data (DynamoDB / S3)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| Read capacity: consumed vs provisioned | Timeseries | `sum:aws.dynamodb.consumed_read_capacity_units{tablename:avionpulse-data}.as_rate()`, `avg:aws.dynamodb.provisioned_read_capacity_units{tablename:avionpulse-data}` | Consumed as area, provisioned as a dashed line |
| Write capacity: consumed vs provisioned | Timeseries | `sum:aws.dynamodb.consumed_write_capacity_units{tablename:avionpulse-data}.as_rate()`, `avg:aws.dynamodb.provisioned_write_capacity_units{tablename:avionpulse-data}` | Same styling |
| Throttle events | Timeseries | `sum:aws.dynamodb.read_throttle_events{tablename:avionpulse-data}.as_count()`, `sum:aws.dynamodb.write_throttle_events{tablename:avionpulse-data}.as_count()` | Bars, red |
| DynamoDB calls by operation | Timeseries | `sum:avionpulse.ddb.calls{*} by {@aws.operation}.as_count()` | Stacked bars. Needs the span metric from section 4. Scan or GetItem spikes = `full_table_scan` / `n_plus_one` |
| Data-tier errors (logs) | Toplist | Logs source: `service:avionpulse "Request failed"`, count, group by `@error_code`… | If the attribute isn't parsed, use a *Log Stream* widget with the same search instead |

**Group 5 — Async (SQS / EventBridge)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| Queue depth | Timeseries | `max:aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events}` | Line |
| Oldest message age | Timeseries | `max:aws.sqs.approximate_age_of_oldest_message{queuename:avionpulse-events}` | Line, unit s; marker `y = 600` |
| DLQ depth | Query Value | `max:aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events-dlq}` | *last*; `> 0` red, `= 0` green |
| Events processed vs poison | Timeseries | `sum:avionpulse.events.processed{$env}.as_count()`, `sum:avionpulse.events.poison{$env}.as_count()` | Bars, poison in red |
| Queue processing lag | Timeseries | `avg:avionpulse.queue.processing_lag_s{$env}`, `max:avionpulse.queue.processing_lag_s{$env}` | Lines, unit s |
| Simulator runs & errors | Timeseries | `sum:aws.lambda.enhanced.invocations{functionname:avionpulse-simulator}.as_count()`, `sum:aws.lambda.enhanced.errors{functionname:avionpulse-simulator}.as_count()` | Bars. Steady ~1 per 5 min is healthy |

**Group 6 — Alerts & logs**

| Widget | Type | Settings |
|---|---|---|
| AvionPulse monitors | Monitor Summary | Query `tag:app:avionpulse`; display *Counts and list*; hide zero counts |
| Error logs | Log Stream | Search `service:avionpulse status:error`; columns *date, functionname, message* |
| Chaos activity | Log Stream | Search `service:avionpulse "[CHAOS]"` — every fault toggle and auto-revert |
| Log volume by status | Timeseries | Data source *Logs*, search `service:avionpulse`, count, group by `status`; stacked bars |

**Finishing touches**

* **Event overlay:** on any timeseries widget → *Event Overlays* → search `tags:app:avionpulse`
  (monitor alerts) so alert transitions appear as vertical bars.
* **Time frame:** save the dashboard on *Past 1 hour* for demos; during a chaos run *Past 15 minutes*
  with live mode on.
* **Note widget** at the top (optional): "Toggle faults at https://\<site\>/#chaos — every fault
  auto-reverts after its TTL."

---

## 8. Verifying everything end to end

1. Deploy with Datadog enabled; browse every page once as `admin`.
2. **Serverless view** shows four functions with traces → instrumentation OK.
3. **Metrics Explorer**: `avionpulse.flights.airborne` has points every 5 min → custom metrics OK.
4. **Logs**: `service:avionpulse` returns JSON logs with `dd.trace_id` → logs and trace linking OK.
5. Wait about 15 minutes, then check `aws.dynamodb.consumed_read_capacity_units{tablename:avionpulse-data}`
   and `aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events}` have data
   → AWS integration OK.
6. Work through the summary table in 6.7 one fault at a time (fault on → alert → fault off →
   recovery). The dashboard's *Chaos switches* bar and *Chaos activity* log stream give you the
   exact time each fault started, to measure time-to-detect.
7. When done: **RESET ALL** on the chaos page, and purge the DLQ in the SQS console.
