# AvionPulse — Serverless Operations (AvionDash Suite)

> A deliberately small AWS application built to generate realistic **serverless** telemetry —
> traces, logs, custom metrics, errors, latency and chaos faults — so you can exercise Datadog's
> serverless monitoring, dashboards and alerting without standing up a large or expensive system.

AvionPulse is the third member of the AvionDash suite, after **AvionDash RHEL** (Apache + PHP +
MariaDB on a VM) and **AvionDash Docker** (Nginx + FastAPI + MySQL in containers). It keeps the
same airline-operations premise and the same cyberpunk NOC look, and runs almost entirely inside
the AWS free tier.

Like its siblings, it is complex enough to produce *authentic* signals: real DynamoDB queries,
real SQS redrives, real API Gateway throttling, real IAM denials, real Lambda timeouts and
out-of-memory kills, rather than a "hello world" that never breaks.

---

## Architecture

```
               ┌──────────────────── WEB TIER ─────────────────────┐
 Browser ────► │ CloudFront ──(/*)──► S3 (SPA, data/notams.json)   │
   (RUM)       │     │                                              │
               └─────┼──────────────────────────────────────────────┘
                     │ /api/*
               ┌─────▼──────────────── APP TIER ───────────────────┐
               │ API Gateway HTTP API ──► λ api (routes, auth,     │
               │   (stage throttle)          chaos engine)          │
               │ EventBridge (5 min) ──► λ simulator (flights,      │
               │                           business metrics, janitor)│
               └─────┬───────────────────────────┬─────────────────┘
               ┌─────▼──────── DATA & ASYNC TIER ▼─────────────────┐
               │ DynamoDB (single table, 5 RCU/5 WCU)               │
               │ SQS events ──► λ worker ──► DynamoDB               │
               │     └── redrive ×3 ──► SQS DLQ                     │
               └────────────────────────────────────────────────────┘
        All Lambdas ──► Datadog Lambda Extension (traces · logs · metrics)
```

| Tier | AvionDash RHEL | AvionDash Docker | **AvionPulse (AWS)** |
|---|---|---|---|
| Web | Apache httpd | Nginx container | **CloudFront + S3** |
| App | PHP-FPM | FastAPI container | **API Gateway HTTP API + Lambda (Python 3.12, arm64)** |
| Data | MariaDB | MySQL container | **DynamoDB + SQS (+ DLQ)** |
| Background | cron | — | **EventBridge schedule → simulator Lambda** |
| Chaos | 20 PHP faults | 14 container faults | **23 AWS-native faults (incl. 1 composite), auto-reverting** |

## Pages

| Page | What it shows | AWS path exercised |
|---|---|---|
| Login | Radar, live public stats, demo users | `POST /api/auth/token` (PBKDF2 + JWT) |
| Dashboard | KPIs, live flight map (Robinson), serverless health, busiest origins, station weather | DynamoDB queries, cold-start flag, data freshness, synthetic METAR |
| Flights | 25 flights cycling scheduled → boarding → departed → en route → landed | DynamoDB `Query` (or `Scan` / N+1 under chaos) |
| Fleet / Airports | Registry tables | DynamoDB `Query` |
| Ops Pipeline | Raise a maintenance request → 202 → SQS → worker → DynamoDB; live queue and DLQ depth | SQS, partial batch failures, redrive |
| NOTAMs | Notice board read from S3 on every request | S3 `GetObject` |
| Architecture & Cost | Diagram, live info about the Lambda container serving you, monthly cost model | `/api/system` |
| Chaos Control | 23 faults across 6 tiers, TTL picker, reset | API Gateway, Lambda, SQS control planes |

Demo users: `admin / aviondash123` (chaos), `operator / operator123` (can raise maintenance
requests), `viewer / viewer123` (read only).

## Quick start

### Run locally (no AWS account needed)

```bash
python scripts/local_server.py
```

Open http://localhost:8080. DynamoDB, SQS and the AWS control plane are in-memory fakes, the
simulator runs every 60 s and the worker drains the fake queue, so every page and every chaos
toggle works. Infra faults are simulated rather than applied.

### Deploy to AWS

Three ways, same result. **Full step-by-step guide: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).**

| Path | Template | You need |
|---|---|---|
| **A. AWS Console** | [`cloudformation/avionpulse.yaml`](cloudformation/avionpulse.yaml) (plain CloudFormation) | Python to build one zip, then a browser |
| **B. CLI, one command** | same | AWS CLI v2 + Python |
| **C. AWS SAM** | [`template.yaml`](template.yaml) | AWS CLI v2 + SAM CLI |

```powershell
./scripts/deploy-cfn.ps1 -AdminPassword 'Choose-A-Long-One!'                       # path B
./scripts/deploy-cfn.ps1 -DatadogApiKey $env:DD_API_KEY -BudgetEmail you@example.com
```
```bash
ADMIN_PASSWORD='Choose-A-Long-One!' ./scripts/deploy-cfn.sh                          # path B
```

The plain CloudFormation template publishes the website itself (a custom resource copies the
SPA into S3 and invalidates CloudFront) and empties the bucket on delete, so a stack created
from the console needs no further steps. A first deploy takes about 5–8 minutes, mostly CloudFront.

### Generate traffic

```bash
python scripts/loadgen.py https://dxxxx.cloudfront.net --rps 0.5 --minutes 30
```

### Tear down

```bash
./scripts/teardown.sh        # or scripts\teardown.ps1
```

## Cost

| | Free tier active | No free tier |
|---|---|---|
| **Typical demo month** (~100k API calls, simulator 24/7) | **≈ $0.00 – $0.15** | **≈ $3.60** |

The only line that matters without free tier is DynamoDB provisioned capacity (~$2.85).
No VPC, NAT gateway, load balancer or RDS. API stage throttle, 3-day log retention, fault
TTLs and an optional AWS Budget alert cap the downside. Full breakdown in
**[docs/COST.md](docs/COST.md)**.

## Chaos faults

| Tier | Faults |
|---|---|
| Edge (CloudFront / API Gateway) | `api_throttling`†, `latency_spike`, `random_500s`, `health_check_fail` |
| Compute (Lambda) | `lambda_timeout`, `lambda_oom`, `memory_leak`, `cpu_burn`, `cold_start_storm`†, `worker_throttle`† |
| Data (DynamoDB / S3) | `dynamodb_throttle`, `full_table_scan`, `n_plus_one`, `iam_access_denied`, `s3_missing_object` |
| Async (SQS / EventBridge) | `sqs_poison_pill`, `queue_backlog`†, `simulator_crash`, `downstream_timeout` |
| Business | `silent_failure`, `pax_corruption`, `log_flood` |
| Composite | `cascading_failure` |

† changes a real AWS resource (stage throttle, function config, reserved concurrency, event
source mapping) and restores it when switched off or when its TTL expires.

Details, Datadog signals and demo storylines: **[docs/FAULT_SCENARIOS.md](docs/FAULT_SCENARIOS.md)**.
Instrumentation, per-fault monitors and the health dashboard: **[docs/DATADOG_SETUP.md](docs/DATADOG_SETUP.md)**.

## Repository layout

```
template.yaml          SAM template: all three tiers, IAM, log groups, optional budget
cloudformation/
  avionpulse.yaml      Plain CloudFormation equivalent (console-deployable, publishes the site itself)
src/
  api.py               API Lambda: routing, auth, request-path fault injection
  chaos.py             Fault catalog, shared state, infra toggles, TTL janitor
  simulator.py         Scheduled flight simulator + business metrics
  worker.py            SQS consumer with partial batch failures
  seed.py              CloudFormation custom resource that seeds DynamoDB
  site_deployer.py     Custom resource that publishes web/ to S3 (plain-CloudFormation path)
  cfn.py               Custom-resource response plumbing
  store.py / infra.py  DynamoDB access and AWS control-plane calls (+ local fakes)
  flightsim.py         Deterministic flight lifecycle model
  auth.py              PBKDF2 passwords + HS256 JWT (standard library only)
web/                   SPA (index.html, static/css, static/js), data/notams.json
scripts/               deploy (SAM) / deploy-cfn / teardown (PowerShell + bash),
                       package.py, local_server.py, loadgen.py
docs/                  DEPLOYMENT.md, COST.md, FAULT_SCENARIOS.md, DATADOG_SETUP.md,
                       datadog/avionpulse-dashboard.json
```

## Notes

* Chaos toggles that change AWS resources create CloudFormation drift; the next `deploy` (or
  RESET ALL) puts everything back to the template's values.
* New AWS accounts that can't reserve concurrency still get `worker_throttle`: setting reserved
  concurrency to 0 doesn't take from the unreserved pool. If AWS refuses any infra change, the
  chaos page shows the AWS error instead of pretending it worked.
* All data is fictional. Tail numbers use the `N-AVP` prefix so AvionPulse data never collides
  with the other AvionDash apps in a shared Datadog org.
