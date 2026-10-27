# AvionPulse — Chaos Fault Scenarios

The Chaos Control page (`#chaos`, admin only) has **23 faults in 6 tiers** (22 individual faults + 1 composite). Fault state is a single
DynamoDB item (`CONFIG / FAULTS`) shared by every Lambda, cached for 3 seconds.

* **Request-path faults** change what the Lambda code does (sleep, allocate, raise, query differently).
* **Infra faults** (marked *MODIFIES AWS*) call an AWS control-plane API to really change a
  resource, and put it back when you switch them off.
* **Every fault has a TTL** (5–120 min, default 30). The simulator runs a janitor every 5 minutes
  that reverts anything expired, including infra changes. **RESET ALL** reverts every infra fault
  whether or not it's marked active.
* `/api/chaos/*` and `/api/auth/*` are exempt from request-path faults and sit on their own API
  Gateway route with separate throttle limits, so you can always switch things back off.

## Edge tier — CloudFront / API Gateway

| Fault | What happens | Datadog signal | Suggested monitor |
|---|---|---|---|
| `api_throttling` *(infra)* | `apigatewayv2:UpdateStage` sets the `$default` stage to 1 req/s, burst 2. API Gateway returns **429** before Lambda runs. | API Gateway 4xx up while Lambda invocations drop | `sum:aws.apigateway.4xx{apiid:<id>}.as_count() > 20` and a composite with `aws.lambda.invocations` falling |
| `latency_spike` | 2–6 s sleep on every request. | `aws.apigateway.integration_latency`, Lambda duration, APM p95 | `avg(last_5m):p95:trace.aws.lambda{service:avionpulse} > 2` |
| `random_500s` | 35% of requests raise an **unhandled** exception → Lambda Error, API Gateway bare 500. | `aws.lambda.errors`, APM error rate, Error Tracking issue | SLO burn-rate alert on `avionpulse` availability |
| `health_check_fail` | `/api/health` returns 503; everything else is fine. | Synthetics API test fails | Synthetics test on `/api/health` |

## Compute tier — Lambda

| Fault | What happens | Datadog signal |
|---|---|---|
| `lambda_timeout` | 30% of requests sleep past the 10 s timeout → `Task timed out`, API Gateway 503. | `aws.lambda.enhanced.timeouts`, duration pinned at 10 s |
| `lambda_oom` | 25% of requests allocate 32 MB chunks until the 256 MB runtime is killed (`Runtime.OutOfMemory`). | `aws.lambda.enhanced.out_of_memory`, `max_memory_used` at limit |
| `memory_leak` | Each request keeps 8 MB alive in a module-level list. Memory climbs per warm container until it OOMs and recycles — a sawtooth. | `aws.lambda.enhanced.max_memory_used` (forecast monitor) |
| `cpu_burn` | 400 ms busy-wait per request. 256 MB Lambdas get a fraction of a vCPU, so it shows as duration. | `aws.lambda.enhanced.billed_duration`, `estimated_cost` |
| `cold_start_storm` *(infra)* | `lambda:UpdateFunctionConfiguration` changes an env var (every warm container is replaced) and sets a 2.5 s sleep in init. Repeats every simulator run while active. The topbar badge shows **COLD START**. | `aws.lambda.enhanced.init_duration`, `cold_start:true` spans |
| `worker_throttle` *(infra)* | `lambda:PutFunctionConcurrency` = 0 on the SQS worker. Polls are throttled, the queue grows. | `aws.lambda.throttles{functionname:avionpulse-worker}`, queue depth |

## Data tier — DynamoDB / S3

| Fault | What happens | Datadog signal |
|---|---|---|
| `dynamodb_throttle` | Every data request runs 25 strongly-consistent full-table Scans against the 5 RCU table. Burst credit drains, then **real** `ProvisionedThroughputExceededException` → 503. | `aws.dynamodb.read_throttle_events`, `consumed_read_capacity_units` > provisioned, APM error type |
| `full_table_scan` | Flights endpoint uses `Scan` + filter instead of `Query`. Same response, more RCUs. | APM span name changes from `Query` to `Scan`; RCU climbs |
| `n_plus_one` | A `GetItem` per flight for its aircraft and origin airport — 50+ DynamoDB spans per request. | Trace waterfall; span count per trace |
| `iam_access_denied` | Dashboard calls `DescribeTable` on `<stack>-data-restricted-ledger`, which the role has no rights to. **Real** `AccessDeniedException`. | APM error `AccessDeniedException`; log pattern `is not authorized to perform` |
| `s3_missing_object` | NOTAM board reads `data/notams-v2.json`, which was never uploaded. S3 `NoSuchKey` → 502. (The role has `s3:ListBucket`, so S3 answers 404, not 403.) | S3 span error `NoSuchKey`; NOTAM page shows the error card |

## Async tier — SQS / EventBridge

| Fault | What happens | Datadog signal |
|---|---|---|
| `sqs_poison_pill` | API (50% of data requests) and simulator (5 per run) enqueue malformed messages. The worker reports them as batch item failures; SQS redelivers 3× then moves them to the **DLQ**. | `aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events-dlq} > 0`, log `Poison message` |
| `queue_backlog` *(infra)* | `lambda:UpdateEventSourceMapping Enabled=false` pauses the worker trigger, and the simulator floods 20 extra events per run. | `aws.sqs.approximate_age_of_oldest_message` climbing |
| `simulator_crash` | The scheduled simulator writes half the flights and raises. Lambda async retry fires once more; data goes stale; business metrics stop. Dashboard "Data freshness" turns red. | `aws.lambda.errors{functionname:avionpulse-simulator}`; **no data** on `avionpulse.flights.airborne` |
| `downstream_timeout` | Weather panel calls a "third-party METAR provider" at an unroutable address. Each call hangs 3 s → 504. | APM `http.request` span timing out; Error Tracking groups `URLError` |

## Business logic — silent failures

| Fault | What happens | Why it matters |
|---|---|---|
| `silent_failure` | The flights query uses partition key `FLIGHTS` instead of `FLIGHT`. Valid query, zero rows, **HTTP 200, no errors, normal latency**. The board is empty, airborne = 0. | Infrastructure and APM stay green. Only a business-metric monitor (`avionpulse.flights.airborne == 0`), `avionpulse.api.flights_returned`, or a Synthetics assertion on page content catches it. |
| `pax_corruption` | Passenger counts ×847 in responses and metrics. | Anomaly monitor on `avionpulse.pax.airborne`. Nothing technical is wrong. |
| `log_flood` | 50 WARNING lines per request. | Log volume anomaly. **The one fault that can cost real money if left on** (see [COST.md](COST.md)). |

## Composite

| Fault | Members |
|---|---|
| `cascading_failure` | `latency_spike` + `dynamodb_throttle` + `random_500s` + `queue_backlog` + `log_flood` |

## Demo storylines

1. **"Green dashboards, broken app"** — turn on `silent_failure`. Show APM: 0% errors, normal
   latency. Show the Flights page: empty. Then show the business metric monitor firing.
2. **"Is it us or AWS?"** — turn on `api_throttling`. Users see errors, but Lambda invocations
   *drop*. API Gateway 4xx and a composite monitor tell the real story.
3. **"The queue nobody watches"** — turn on `sqs_poison_pill`. The UI works perfectly; the DLQ
   counter on the Ops Pipeline page climbs; the DLQ monitor fires.
4. **"Cold starts after a deploy"** — turn on `cold_start_storm` and refresh the dashboard. Every
   container is new; show `init_duration` and the cold-start spans in the trace flame graph.
5. **"The tightened policy"** — `iam_access_denied`. A real `AccessDeniedException` with the exact
   ARN in the error message; Error Tracking groups it as a new issue.
