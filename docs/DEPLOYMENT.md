# AvionPulse — Deploying to AWS

This guide covers everything from an empty AWS account to a running AvionPulse, then updating,
tearing down and troubleshooting it.

**Contents**

1. [Choose a deployment path](#1-choose-a-deployment-path)
2. [Before you start](#2-before-you-start) — account, permissions, region, tools, cost guard
3. [Optional: store the Datadog API key](#3-optional-store-the-datadog-api-key)
4. [Path A — AWS Console + CloudFormation](#4-path-a--aws-console--cloudformation-no-cli-needed-for-the-stack)
5. [Path B — AWS CLI + CloudFormation](#5-path-b--aws-cli--cloudformation-one-command)
6. [Path C — AWS SAM](#6-path-c--aws-sam)
7. [Parameter reference](#7-parameter-reference)
8. [What gets created](#8-what-gets-created)
9. [Verify the deployment](#9-verify-the-deployment)
10. [Day-2 operations](#10-day-2-operations) — code updates, reseeding, enabling Datadog, passwords
11. [Securing a shared deployment](#11-securing-a-shared-deployment)
12. [Tear down](#12-tear-down)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Choose a deployment path

All three paths create the same application. They differ in tooling and in how the web files reach S3.

| | **A. Console + CloudFormation** | **B. CLI + CloudFormation** | **C. AWS SAM** |
|---|---|---|---|
| Template | [`cloudformation/avionpulse.yaml`](../cloudformation/avionpulse.yaml) | same | [`template.yaml`](../template.yaml) (SAM transform) |
| Tools on your PC | Python 3.10+ (to build the zip) and a browser | AWS CLI v2 + Python | AWS CLI v2 + SAM CLI |
| How code gets to Lambda | You upload one zip to S3 | Script builds and uploads the zip | `sam deploy` packages `src/` |
| How the website gets to S3 | `SiteDeployment` custom resource (automatic) | same (automatic) | Deploy script runs `aws s3 sync` |
| Stack delete empties the bucket | Yes (custom resource) | Yes | Teardown script empties it |
| Best for | First-timers; seeing every parameter in the console; locked-down machines | Repeatable deploys without SAM | Developers already using SAM |

If you're unsure, use **Path B**: one command on Windows or macOS/Linux. Use **Path A** if you'd rather click through the AWS console.

---

## 2. Before you start

### 2.1 AWS account

* Use a **sandbox or personal account**, not production. The chaos engine changes Lambda,
  API Gateway and SQS settings on purpose. It only touches resources named after this stack,
  but a demo account keeps any surprises contained.
* Make sure you know which account you're in:
  ```bash
  aws sts get-caller-identity
  ```

### 2.2 Permissions for the person deploying

The easiest option is `AdministratorAccess` in a sandbox account. For least privilege, the
deploying identity needs create/update/delete rights on:

| Service | Why |
|---|---|
| CloudFormation | Create and update the stack |
| IAM | Create 2 roles and 1 managed policy (`CAPABILITY_IAM`) and pass them to Lambda (`iam:PassRole`) |
| Lambda | Functions, event source mapping, permissions, event invoke config, and reading Datadog's public layers |
| API Gateway (v2) | HTTP API, routes, integration, stage |
| DynamoDB, SQS | Table and queues |
| S3 | Site bucket (+ the artifact bucket for paths A/B) |
| CloudFront | Distribution and Origin Access Control |
| CloudWatch Logs | Log groups with retention |
| EventBridge | Scheduled rule |
| SSM Parameter Store | Only if you store a Datadog key |
| Budgets | Only if you set `BudgetEmail` |

### 2.3 Region

* **Recommended: `us-east-1`.** Every service used is available there, CloudFront metrics are
  published there, and Datadog's Lambda layers are published there.
* Any commercial region with Lambda arm64 (Graviton) and HTTP APIs works. The Datadog layers are
  published per region, so check the region is listed in Datadog's docs if you plan to enable it.
* The **artifact bucket (paths A/B) must be in the same region as the stack**. Lambda can only
  load code from a bucket in its own region.

### 2.4 Tools

| Tool | Needed for | Install (Windows) | Check |
|---|---|---|---|
| Python 3.10+ | A, B (builds the zip), local testing | python.org installer, tick *Add to PATH* | `python --version` |
| AWS CLI v2 | B, C, teardown scripts | `msiexec /i https://awscli.amazonaws.com/AWSCLIV2.msi` | `aws --version` |
| AWS SAM CLI | C only | MSI from the *AWS SAM CLI* install page | `sam --version` |

Then configure credentials, either with access keys:

```bash
aws configure
```

or with IAM Identity Center (SSO):

```bash
aws configure sso
```
```bash
aws sso login --profile <your-profile>
```

With SSO, set `AWS_PROFILE=<your-profile>` (PowerShell: `$env:AWS_PROFILE="<your-profile>"`)
before running the scripts. Set `AWS_REGION` the same way if you don't use `us-east-1`.

### 2.5 Try it locally first (optional, 30 seconds)

```bash
python scripts/local_server.py
```

Open http://localhost:8080. It's the full UI and chaos panel against in-memory fakes, a good way to
check the pages work before you spend anything on AWS.

### 2.6 Cost guard

Expected cost is about **$0.00–$0.15 per month** while the free tier applies, and about
**$3.60 per month** without it (details in [COST.md](COST.md)). To be emailed if spend goes over a
limit, pass `BudgetEmail` (all paths). AWS sends a confirmation email the first time; it creates a
monthly AWS Budget that alerts at 50% actual and 100% forecast of `MonthlyBudgetUsd` (default $5).

---

## 3. Optional: store the Datadog API key

Skip this section to deploy without Datadog. You can enable it later (section 10.3).

The template never takes the API key as a parameter. It reads it from an SSM **SecureString**
(standard tier, free). The scripts in paths B and C create this for you when you pass the key. For
path A, create it yourself:

**Console:** *Systems Manager → Parameter Store → Create parameter*
* Name: `/avionpulse/datadog/api-key` (must match the `DatadogApiKeySsmName` parameter)
* Tier: **Standard**
* Type: **SecureString**, KMS key source *My current account*, key `alias/aws/ssm`
* Value: your Datadog API key (*Organization Settings → API Keys* in Datadog)

**CLI:**
```bash
aws ssm put-parameter --name /avionpulse/datadog/api-key --type SecureString --value <DD_API_KEY> --region us-east-1
```

**Layer versions.** Find the current version numbers of `Datadog-Python312-ARM` and
`Datadog-Extension-ARM` in Datadog's *Serverless → AWS Lambda → Python* installation docs. The
template defaults (107 / 80) are placeholders. A version that doesn't exist fails the deploy with
`Layer version ... does not exist`.

---

## 4. Path A — AWS Console + CloudFormation (no CLI needed for the stack)

### Step A1 — Build the code package

From the `AvionPulse_AWS` folder:

```bash
python scripts/package.py
```

Output looks like:

```text
built dist/avionpulse-code-cb6b12dcc1.zip (115 KB)
CodeKey=avionpulse/avionpulse-code-cb6b12dcc1.zip
```

The zip holds the Lambda code at its root and the website under `web/`. The hash in the name
changes whenever any file changes, which is how CloudFormation knows to roll out new code later.

### Step A2 — Create an artifact bucket and upload the zip

1. **S3 console → Create bucket**
   * **Bucket name:** globally unique, e.g. `avionpulse-artifacts-<your-account-id>-us-east-1`
   * **Region:** the region you'll deploy the stack to
   * **Block all public access:** leave **on**
   * Everything else default → **Create bucket**
2. Open the bucket → **Create folder** `avionpulse` → open it → **Upload** →
   add `dist/avionpulse-code-<hash>.zip` → **Upload**.
3. Note the bucket name and the key `avionpulse/avionpulse-code-<hash>.zip`. You'll need both in the next step.

### Step A3 — Create the stack

1. **CloudFormation console** (check the region selector top-right) → **Create stack → With new
   resources (standard)**.
2. **Prerequisite:** *Choose an existing template*. **Specify template:** *Upload a template file*
   → **Choose file** → `cloudformation/avionpulse.yaml` → **Next**.
3. **Stack name:** `avionpulse`. Use lowercase letters, digits and hyphens, ≤ 30 characters. Resource
   names (`avionpulse-api`, `avionpulse-data`…) and the Datadog queries in the docs derive from it.
4. **Parameters** (grouped as on screen; full list in [section 7](#7-parameter-reference)):
   * **1. Code package** — `CodeBucket` = your artifact bucket; `CodeKey` = `avionpulse/avionpulse-code-<hash>.zip`
   * **2. Application** — set `AdminPassword` if anyone else can reach the site (section 11);
     leave the rest default.
   * **3. Datadog** — `EnableDatadog=true` only if you did section 3; set the two layer versions.
     Optionally the RUM id/token.
   * **4. Cost guards** — defaults are fine; optionally `BudgetEmail`.
   * **Next.**
5. **Configure stack options:**
   * **Tags:** add `app = avionpulse` (optional; every resource is tagged anyway).
   * **Stack failure options:** keep *Roll back all stack resources*.
   * **Next.**
6. **Review:** at the bottom tick **"I acknowledge that AWS CloudFormation might create IAM
   resources."** → **Submit**.

### Step A4 — Watch it build (about 5–8 minutes)

Open the **Events** tab and refresh. The order is roughly:

| Minute | What happens |
|---|---|
| 0–1 | Table, queues, buckets, IAM roles, log groups |
| 1–2 | Lambda functions, HTTP API, routes, stage, EventBridge rule, SQS trigger |
| 1–2 | `SeedData` runs the seeder (~20 s: 70 items, paced for 5 WCU) |
| 2–6 | **CloudFront distribution** (the slow part) |
| 5–7 | `SiteDeployment` copies the website into S3 and invalidates CloudFront |
| ~7 | Stack status **CREATE_COMPLETE** |

### Step A5 — Open the site

**Outputs** tab → **SiteUrl** (`https://dxxxxxxxxxxxx.cloudfront.net`). Sign in with `admin` and
either your `AdminPassword` or the demo password `aviondash123`. Then go to [section 9](#9-verify-the-deployment).

---

## 5. Path B — AWS CLI + CloudFormation (one command)

The script does path A for you: it creates the artifact bucket if missing, builds and uploads the
zip, stores the Datadog key, deploys `cloudformation/avionpulse.yaml` and prints the URL.

**PowerShell**
```powershell
./scripts/deploy-cfn.ps1
./scripts/deploy-cfn.ps1 -AdminPassword 'Choose-A-Long-One!' -BudgetEmail you@example.com
./scripts/deploy-cfn.ps1 -DatadogApiKey $env:DD_API_KEY -DatadogPythonLayerVersion <n> -DatadogExtensionLayerVersion <n>
```

**bash**
```bash
./scripts/deploy-cfn.sh
ADMIN_PASSWORD='Choose-A-Long-One!' BUDGET_EMAIL=you@example.com ./scripts/deploy-cfn.sh
DD_API_KEY=xxx DD_PY_LAYER=<n> DD_EXT_LAYER=<n> ./scripts/deploy-cfn.sh
```

| PowerShell parameter | bash variable | Default |
|---|---|---|
| `-StackName` | `STACK_NAME` | `avionpulse` |
| `-Region` | `AWS_REGION` | `us-east-1` |
| `-CodeBucket` | `CODE_BUCKET` | `<stack>-artifacts-<account>-<region>` (created if missing) |
| `-AppEnv` | `APP_ENV` | `demo` |
| `-AdminPassword` | `ADMIN_PASSWORD` | demo password |
| `-DatadogApiKey` | `DD_API_KEY` | none → Datadog off |
| `-DatadogSite` | `DD_SITE` | `datadoghq.com` |
| `-DatadogPythonLayerVersion` / `-DatadogExtensionLayerVersion` | `DD_PY_LAYER` / `DD_EXT_LAYER` | 107 / 80 (placeholders) |
| `-RumApplicationId` / `-RumClientToken` | `RUM_APPLICATION_ID` / `RUM_CLIENT_TOKEN` | none |
| `-BudgetEmail` / `-MonthlyBudgetUsd` | `BUDGET_EMAIL` / `MONTHLY_BUDGET_USD` | none / 5 |

Re-running the same command updates the stack in place. If nothing changed it exits cleanly
(`--no-fail-on-empty-changeset`). On failure it prints the first failed resources and their reasons.

**Doing it by hand** (what the script runs):

```bash
python scripts/package.py --bucket <artifact-bucket> --region us-east-1
```
```bash
aws cloudformation deploy --region us-east-1 --stack-name avionpulse --template-file cloudformation/avionpulse.yaml --capabilities CAPABILITY_IAM --parameter-overrides CodeBucket=<artifact-bucket> CodeKey=avionpulse/avionpulse-code-<hash>.zip
```
```bash
aws cloudformation describe-stacks --stack-name avionpulse --query "Stacks[0].Outputs" --output table
```

---

## 6. Path C — AWS SAM

```powershell
./scripts/deploy.ps1
./scripts/deploy.ps1 -AdminPassword 'Choose-A-Long-One!' -DatadogApiKey $env:DD_API_KEY -BudgetEmail you@example.com
```
```bash
./scripts/deploy.sh
ADMIN_PASSWORD='...' DD_API_KEY=xxx BUDGET_EMAIL=you@example.com ./scripts/deploy.sh
```

`sam deploy --resolve-s3` creates a SAM-managed artifact bucket on first use, deploys
`template.yaml`, then the script syncs `web/` to the site bucket, writes `config.js` and
invalidates CloudFront.

> Don't deploy **both** templates under the same stack name. They define the same resources
> differently (SAM generates its own IAM role names, for example). Pick one path per stack. To switch,
> tear down first.

---

## 7. Parameter reference

Applies to `cloudformation/avionpulse.yaml`. `template.yaml` has the same parameters except
`CodeBucket`/`CodeKey` and the two RUM parameters (the SAM scripts handle those).

| Parameter | Default | What it does / when to change it |
|---|---|---|
| `CodeBucket` | — (required) | S3 bucket in the stack's region holding the code zip |
| `CodeKey` | `avionpulse/avionpulse-code.zip` | Key of the zip. Use the hashed name `package.py` prints; a new key rolls out new code |
| `AppEnv` | `demo` | `env` tag on metrics, traces and logs. Lowercase, ≤ 20 chars |
| `AppVersion` | `1.0.0` | `version` tag (Datadog deployment tracking) |
| `AdminPassword` | blank | Replaces `aviondash123` for `admin`. NoEcho (masked in the console). Strongly recommended for shared deployments |
| `SeedVersion` | `1` | Change it (e.g. to `2`) to reseed the table on the next update. Resets flights and users; keeps the JWT secret |
| `EnableDatadog` | `false` | Attaches the Datadog layers and handler wrapper |
| `DatadogSite` | `datadoghq.com` | Your Datadog site (EU: `datadoghq.eu`, etc.) |
| `DatadogApiKeySsmName` | `/avionpulse/datadog/api-key` | Where the extension reads the API key |
| `DatadogPythonLayerVersion` | 107 | `Datadog-Python312-ARM` version, check Datadog docs |
| `DatadogExtensionLayerVersion` | 80 | `Datadog-Extension-ARM` version, check Datadog docs |
| `RumApplicationId` / `RumClientToken` | blank | Turns on Datadog RUM in the browser |
| `ApiRateLimit` / `ApiBurstLimit` | 20 / 40 | HTTP API default-route throttle: a hard ceiling on spend |
| `FaultTtlMinutes` | 30 | Default auto-revert time for chaos faults (1–120) |
| `LogRetentionDays` | 3 | Retention for all 6 log groups |
| `SimulatorSchedule` | `rate(5 minutes)` | How often flights move and business metrics are emitted |
| `BudgetEmail` / `MonthlyBudgetUsd` | blank / 5 | Optional AWS Budget alert |

---

## 8. What gets created

`cloudformation/avionpulse.yaml` creates 35 resources (`template.yaml` creates the equivalent via SAM):

| Tier | Resources |
|---|---|
| Web | S3 site bucket (private, encrypted), bucket policy (CloudFront OAC only), Origin Access Control, CloudFront distribution (`/*` → S3, `/api/*` → API Gateway) |
| App | HTTP API, Lambda proxy integration, 3 routes (`/api/{proxy+}`, `/api/chaos/{proxy+}`, `/api/auth/{proxy+}`), `$default` stage with throttling and access logs, `api` / `worker` / `simulator` Lambdas, EventBridge rule + permission, async-retry config |
| Data / async | DynamoDB table (5 RCU / 5 WCU, TTL), SQS queue + DLQ (redrive after 3), SQS → worker event source mapping |
| IAM | `AppRole` (app Lambdas), `SiteDeployerRole`, `ChaosInfraPolicy` (managed policy scoped to this stack's resource names) |
| Ops | 6 log groups with retention, `seed` and `site-deployer` Lambdas with their custom resources, optional Budget |

Names follow `<stack>-api`, `<stack>-worker`, `<stack>-simulator`, `<stack>-data`, `<stack>-events`,
`<stack>-events-dlq`. The site bucket gets an auto-generated name (see the `BucketName` output).

---

## 9. Verify the deployment

Replace `<site>` with the `SiteUrl` output.

| # | Check | How | Expected |
|---|---|---|---|
| 1 | Stack | CloudFormation → stack status | `CREATE_COMPLETE` / `UPDATE_COMPLETE` |
| 2 | Edge + API | `curl https://<site>/api/health` | `{"status": "ok", "service": "avionpulse-api", ...}` |
| 3 | All tiers | `curl https://<site>/api/health/deep` | `dynamodb`, `sqs`, `s3` all `"ok"` |
| 4 | Data seeded | `curl https://<site>/api/dashboard/public-stats` | `flights.total` 25, `aircraft.total` 15, `airports.total` 25 |
| 5 | Web tier | Open `https://<site>` | Login page with radar animation and live stats |
| 6 | Auth | Sign in as `admin` | Dashboard with KPIs, flight map, weather |
| 7 | Simulator | Wait 5 min, refresh Dashboard | *Data freshness* shows "0–5 min old" |
| 8 | Async pipeline | *Ops Pipeline* → raise a request | 202 Accepted; the event appears in the table within ~10 s |
| 9 | S3 read path | *NOTAMs* page | 8 NOTAM cards |
| 10 | Chaos | *Chaos Control* → toggle **Latency Spike** on, browse, toggle off | Pages slow to 2–6 s, then recover; topbar shows active-fault badge |
| 11 | Infra chaos | Toggle **Worker Concurrency = 0** on | No error banner. `aws lambda get-function-concurrency --function-name avionpulse-worker` shows 0. Toggle off → reservation removed |
| 12 | Logs | CloudWatch → Log groups → `/aws/lambda/avionpulse-api` | JSON log lines |

If you enabled Datadog, continue with [DATADOG_SETUP.md](DATADOG_SETUP.md) section 8.

---

## 10. Day-2 operations

### 10.1 Deploy a code change

* **Path B:** re-run `deploy-cfn`. A changed file gives a new zip hash and a new `CodeKey`, so all
  functions update and the site is republished.
* **Path A:** `python scripts/package.py` → upload the new zip → stack → **Update** → *Use current
  template* → change `CodeKey` to the new name → Next → Next → tick the IAM box → **Submit**.
* **Path C:** re-run `deploy.ps1` / `deploy.sh`.

### 10.2 Reseed the data

Change `SeedVersion` (e.g. `1` → `2`) and update the stack. Users, flights, aircraft and
airports are rewritten; processed events and the JWT secret are kept.

### 10.3 Turn Datadog on (or off) later

1. Create the SSM parameter (section 3) if it doesn't exist.
2. Update the stack with `EnableDatadog=true` and the two layer versions (Path A: *Update → Use
   current template*; Path B/C: re-run with the API key option).
3. Functions switch to the Datadog handler wrapper on the next invocation. Set it back to `false`
   to remove the layers.

### 10.4 Change the admin password

Update the stack with a new `AdminPassword`. The password is one of the seeder's properties, so
changing it reruns the seeder (which also rewrites flights and the other demo users, harmlessly).
To go back to the demo password, clear `AdminPassword` and bump `SeedVersion` in the same update.
Existing sessions stay valid until their 8-hour token expires.

### 10.5 After chaos experiments

Faults revert themselves after their TTL. To clean up immediately: **Chaos Control → RESET ALL**.
It restores the stage throttle, worker concurrency, SQS trigger and API function config. Messages
left in the DLQ stay for 4 days; purge them in the SQS console if a monitor keeps alerting.

> Infra faults change resources outside CloudFormation, which shows up as **drift**. Any stack
> update puts the template values back, and so does RESET ALL.

---

## 11. Securing a shared deployment

The site is public on a `*.cloudfront.net` URL and the demo passwords are published in this repo.
Anyone who signs in as `admin` can run chaos experiments, which change AWS resources (all scoped to
this stack, and all auto-reverting). If anyone other than you might find the URL:

| Measure | Cost | How |
|---|---|---|
| **Set `AdminPassword`** | Free | Parameter (section 7). Do this at minimum |
| Tear down between demos | Free | Section 12. A redeploy takes ~7 minutes |
| Keep the API throttle | Free | `ApiRateLimit` / `ApiBurstLimit` cap abuse and cost |
| Budget alert | Free | `BudgetEmail` |
| Restrict by IP with AWS WAF | ~$5+/month | Attach a WAF web ACL with an IP set to the distribution (outside this template, to keep the default free) |

The `operator` and `viewer` demo users can't change anything in AWS (operators can only enqueue
maintenance requests).

---

## 12. Tear down

```powershell
./scripts/teardown.ps1                    # add -StackName / -Region if you changed them
```
```bash
./scripts/teardown.sh
```

The scripts empty the site bucket, delete the stack, wait for completion (~5–15 minutes, mostly
CloudFront) and delete the SSM API-key parameter (`-KeepApiKey` / `KEEP_API_KEY=1` keeps it).

**Console:** CloudFormation → stack → **Delete**. With the plain-CloudFormation template the
`SiteDeployment` custom resource empties the bucket for you. With the SAM template, empty the
bucket in S3 first or the delete fails on `SiteBucket`.

**Left behind on purpose** (pennies, delete them if you don't need them):
* The artifact bucket (`avionpulse-artifacts-…`) for paths A/B, or `aws-sam-cli-managed-default-…` for path C
* The SSM parameter, if you kept it
* The AWS Budget is part of the stack and is deleted with it

Check nothing is left with a tag search: *Resource Groups & Tag Editor → Tag Editor →* all
regions, tag `app = avionpulse`.

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Requires capabilities : [CAPABILITY_IAM]` | IAM box not ticked / flag missing | Tick the acknowledgement, or add `--capabilities CAPABILITY_IAM` |
| `Error occurred while GetObject. S3 Error Code: NoSuchKey` / `PermanentRedirect` on a Lambda | Wrong `CodeKey`, or the artifact bucket is in another region | Check the key exactly (folder prefix included); bucket must be in the stack's region |
| `Layer version arn:...Datadog-... does not exist` | Placeholder layer versions | Look up current versions (section 3) and update |
| `SeedData` or `SiteDeployment` **CREATE_FAILED** | The custom resource reported an error | The *Status reason* includes the log stream name. Open `/aws/lambda/<stack>-seed` or `/aws/lambda/<stack>-site-deployer` |
| Stack sits on a custom resource for ~1 hour | The function couldn't start at all (wrong zip, wrong handler), so it never answered | Wait for the timeout or cancel the update; check the function's log group; rebuild the zip with `package.py` (don't zip by hand: modules must be at the zip root) |
| `already exists` for a function, table or queue | A resource with the same name exists (another stack, or a leftover) | Use a different stack name, or delete the leftover |
| Site shows **AccessDenied** XML | CloudFront still propagating, or the site files weren't published | Wait 5 min; check the bucket contains `index.html`; Path C: re-run the deploy script |
| Site loads but every API call fails | CloudFront `/api/*` origin still deploying, or Lambda erroring | `curl <ApiEndpoint>/api/health` directly; check `/aws/lambda/<stack>-api` |
| HTTP **429** everywhere | `api_throttling` fault active, or real traffic above the stage limit | Chaos → turn it off / RESET ALL; raise `ApiRateLimit` |
| HTTP **503** on data pages | DynamoDB throttling (`dynamodb_throttle` fault or heavy load on 5 RCU) | Turn the fault off; it recovers in seconds |
| Chaos toggle shows `⚠ … AccessDenied` or `ResourceConflictException` | Missing permission (stack name mismatch) or a Lambda config update still in progress | Wait 10 s and retry; make sure you didn't rename functions outside the template |
| `DELETE_FAILED` on `SiteBucket` | Bucket not empty (SAM template, or objects added by hand) | Empty it in the S3 console, then delete the stack again |
| Login fails with the demo password | `AdminPassword` was set | Use your `AdminPassword`; or update the stack with it blank + new `SeedVersion` |
| Dashboard *Data freshness* keeps growing | Simulator not running (`simulator_crash` fault, or rule disabled) | Chaos → turn the fault off; EventBridge → Rules → check the rule is *Enabled* |

**Useful commands**

```bash
aws cloudformation describe-stack-events --stack-name avionpulse --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output table
```
```bash
aws logs tail /aws/lambda/avionpulse-api --since 15m --follow
```
```bash
aws lambda invoke --function-name avionpulse-simulator out.json
```
