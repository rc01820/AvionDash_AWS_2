"""AvionPulse chaos engine.

Fault state lives in a single DynamoDB item (CONFIG / FAULTS) so every Lambda
(api, worker, simulator) sees the same switches. Each Lambda caches it for a
few seconds to keep read capacity low.

Two kinds of fault:
  * request-path faults: code behaviour inside the Lambda (sleep, allocate, raise…)
  * infra faults (infra=True): the toggle calls an AWS control-plane API to
    really change a resource (stage throttle, reserved concurrency, event
    source mapping, function configuration). Turning the fault off — or its
    TTL expiring — puts the resource back.

Every fault has a TTL. The simulator Lambda runs janitor() every few minutes
and reverts anything that has expired, so a forgotten experiment can't keep
burning money or alert noise.
"""
import logging
import time

import config
import infra
from store import err_code, get_store

log = logging.getLogger("avionpulse.chaos")

TIERS = {
    "edge": "EDGE TIER — CloudFront / API Gateway",
    "compute": "COMPUTE TIER — AWS Lambda",
    "data": "DATA TIER — DynamoDB / S3",
    "async": "ASYNC TIER — SQS / EventBridge",
    "business": "BUSINESS LOGIC — silent failures",
    "composite": "COMPOSITE SCENARIOS",
}

CATALOG = {
    # ── Edge ─────────────────────────────────────────────────────────────
    "api_throttling": {
        "label": "API Gateway Throttling", "tier": "edge", "severity": "critical", "aws": "API Gateway",
        "infra": True, "icon": "🚦",
        "description": "Calls apigatewayv2:UpdateStage to drop the $default stage to 1 req/s (burst 2). "
                       "Browsers start receiving HTTP 429 Too Many Requests from API Gateway itself — the Lambda never runs.",
        "datadog_signal": "aws.apigateway.4xx spike while aws.lambda.invocations drops → composite monitor",
    },
    "latency_spike": {
        "label": "Latency Spike", "tier": "edge", "severity": "warning", "aws": "API Gateway + Lambda",
        "infra": False, "icon": "🐢",
        "description": "Adds a 2–6 s sleep to every API request. API Gateway IntegrationLatency and Lambda duration climb together.",
        "datadog_signal": "aws.apigateway.integration_latency p95 / trace.aws.lambda duration anomaly",
    },
    "random_500s": {
        "label": "Unhandled Exceptions", "tier": "edge", "severity": "warning", "aws": "Lambda",
        "infra": False, "icon": "💥",
        "description": "35% of requests raise an unhandled exception. Lambda counts an Error and API Gateway returns a bare 500.",
        "datadog_signal": "aws.lambda.errors + APM error rate → SLO burn-rate alert",
    },
    "health_check_fail": {
        "label": "Health Check Failure", "tier": "edge", "severity": "critical", "aws": "API Gateway",
        "infra": False, "icon": "🩺",
        "description": "/api/health returns 503. Everything else keeps working — only an external probe notices.",
        "datadog_signal": "Synthetics API test FAIL → downtime alert",
    },
    # ── Compute ──────────────────────────────────────────────────────────
    "lambda_timeout": {
        "label": "Lambda Timeout", "tier": "compute", "severity": "critical", "aws": "Lambda",
        "infra": False, "icon": "⏱",
        "description": "30% of requests sleep past the 10 s function timeout. Lambda logs 'Task timed out' and API Gateway answers 503.",
        "datadog_signal": "aws.lambda.enhanced.timeouts → monitor; duration pinned at max",
    },
    "lambda_oom": {
        "label": "Out of Memory", "tier": "compute", "severity": "critical", "aws": "Lambda",
        "infra": False, "icon": "🧨",
        "description": "25% of requests allocate past the 256 MB memory size until the runtime is killed (Runtime.OutOfMemory).",
        "datadog_signal": "aws.lambda.enhanced.out_of_memory + max_memory_used at limit",
    },
    "memory_leak": {
        "label": "Warm-Container Memory Leak", "tier": "compute", "severity": "warning", "aws": "Lambda",
        "infra": False, "icon": "🧠",
        "description": "Each request keeps 8 MB alive in a module-level list. Memory grows per warm container until it OOMs and recycles.",
        "datadog_signal": "aws.lambda.enhanced.max_memory_used sawtooth → forecast monitor",
    },
    "cpu_burn": {
        "label": "CPU Burn", "tier": "compute", "severity": "warning", "aws": "Lambda",
        "infra": False, "icon": "🔥",
        "description": "Busy-waits 400 ms per request. At 256 MB the function gets a fraction of a vCPU, so billed duration jumps.",
        "datadog_signal": "aws.lambda.enhanced.billed_duration / estimated_cost increase",
    },
    "cold_start_storm": {
        "label": "Cold Start Storm", "tier": "compute", "severity": "warning", "aws": "Lambda",
        "infra": True, "icon": "🥶",
        "description": "lambda:UpdateFunctionConfiguration touches an env var (every warm container is replaced) and adds a "
                       "2.5 s sleep to init. Repeats on every simulator run while active.",
        "datadog_signal": "aws.lambda.enhanced.init_duration + cold_start:true spans in traces",
    },
    "worker_throttle": {
        "label": "Worker Concurrency = 0", "tier": "compute", "severity": "critical", "aws": "Lambda + SQS",
        "infra": True, "icon": "⛔",
        "description": "lambda:PutFunctionConcurrency sets the SQS worker's reserved concurrency to 0. Every poll is throttled; messages pile up.",
        "datadog_signal": "aws.lambda.throttles (worker) + aws.sqs.approximate_number_of_messages_visible",
    },
    # ── Data ─────────────────────────────────────────────────────────────
    "dynamodb_throttle": {
        "label": "DynamoDB Read Storm", "tier": "data", "severity": "critical", "aws": "DynamoDB",
        "infra": False, "icon": "🌪",
        "description": "Every data request fires 25 strongly-consistent full-table Scans against a 5 RCU table. "
                       "Burst capacity drains in seconds, then real ProvisionedThroughputExceededException errors surface as 503s.",
        "datadog_signal": "aws.dynamodb.read_throttle_events + consumed_read_capacity_units > provisioned",
    },
    "full_table_scan": {
        "label": "Scan Instead of Query", "tier": "data", "severity": "warning", "aws": "DynamoDB",
        "infra": False, "icon": "🔍",
        "description": "The flights endpoint uses Scan + client-side filter instead of a key Query. Same answer, many more RCUs.",
        "datadog_signal": "APM span aws.dynamodb Scan replaces Query; consumed RCU climbs",
    },
    "n_plus_one": {
        "label": "N+1 GetItem Calls", "tier": "data", "severity": "warning", "aws": "DynamoDB",
        "infra": False, "icon": "🔁",
        "description": "Flights list does a separate GetItem for each row's aircraft and origin airport — 50+ calls instead of 1.",
        "datadog_signal": "Trace waterfall with dozens of identical aws.dynamodb GetItem spans",
    },
    "iam_access_denied": {
        "label": "IAM Permission Denied", "tier": "data", "severity": "critical", "aws": "IAM + DynamoDB",
        "infra": False, "icon": "🔐",
        "description": "The dashboard reads a ledger table the Lambda role has no permission for — a real AccessDeniedException, like a policy someone tightened in a hurry.",
        "datadog_signal": "APM error type AccessDeniedException; log pattern 'is not authorized to perform'",
    },
    "s3_missing_object": {
        "label": "S3 Object Missing", "tier": "data", "severity": "warning", "aws": "S3",
        "infra": False, "icon": "🪣",
        "description": "NOTAM board reads data/notams-v2.json, which was never uploaded. S3 returns NoSuchKey (404) and the panel shows 502.",
        "datadog_signal": "aws.s3 GetObject span error NoSuchKey; aws.s3.4xx_errors (request metrics)",
    },
    # ── Async ────────────────────────────────────────────────────────────
    "sqs_poison_pill": {
        "label": "SQS Poison Messages", "tier": "async", "severity": "warning", "aws": "SQS",
        "infra": False, "icon": "☠",
        "description": "API and simulator enqueue malformed messages. The worker fails them, SQS redelivers 3 times, then moves them to the DLQ.",
        "datadog_signal": "aws.sqs.approximate_number_of_messages_visible on the DLQ > 0 → alert",
    },
    "queue_backlog": {
        "label": "Worker Trigger Paused", "tier": "async", "severity": "critical", "aws": "SQS + Lambda",
        "infra": True, "icon": "📥",
        "description": "lambda:UpdateEventSourceMapping disables the SQS trigger and the simulator floods 20 extra events per run. Queue age climbs steadily.",
        "datadog_signal": "aws.sqs.approximate_age_of_oldest_message threshold monitor",
    },
    "simulator_crash": {
        "label": "Scheduled Job Crash", "tier": "async", "severity": "critical", "aws": "EventBridge + Lambda",
        "infra": False, "icon": "🛑",
        "description": "The EventBridge-scheduled simulator crashes after writing half the flights. Async retries fire, data goes stale, business metrics stop.",
        "datadog_signal": "aws.lambda.errors (simulator) + 'no data' on avionpulse.flights.airborne",
    },
    "downstream_timeout": {
        "label": "Weather API Timeout", "tier": "async", "severity": "warning", "aws": "Lambda (egress)",
        "infra": False, "icon": "⛈",
        "description": "The weather panel calls a third-party METAR provider that never answers. Each call hangs 3 s and fails with 504.",
        "datadog_signal": "APM http.request span to 10.255.255.1 timing out; error tracking groups the timeout",
    },
    # ── Business ─────────────────────────────────────────────────────────
    "silent_failure": {
        "label": "Silent Partition-Key Bug", "tier": "business", "severity": "critical", "aws": "DynamoDB",
        "infra": False, "icon": "🤫",
        "description": "A 'deploy' changes the flights key to FLIGHTS. Queries succeed with zero rows: HTTP 200, no errors, no latency change — "
                       "but the board is empty and airborne count is 0.",
        "datadog_signal": "ONLY business metric avionpulse.flights.airborne == 0 or a Synthetics content assertion catches it",
    },
    "pax_corruption": {
        "label": "Passenger Count Corruption", "tier": "business", "severity": "warning", "aws": "Lambda",
        "infra": False, "icon": "🧮",
        "description": "Passenger counts are multiplied by 847 in responses and metrics. Infrastructure is green; the numbers are absurd.",
        "datadog_signal": "Anomaly monitor on avionpulse.pax.airborne",
    },
    "log_flood": {
        "label": "Log Flood", "tier": "business", "severity": "warning", "aws": "CloudWatch Logs",
        "infra": False, "icon": "📜",
        "description": "Emits 50 WARNING lines per request. Watch log ingestion — this is the one fault that can cost real money if left on.",
        "datadog_signal": "logs volume anomaly; aws.logs.incoming_bytes",
    },
    # ── Composite ────────────────────────────────────────────────────────
    "cascading_failure": {
        "label": "Cascading Failure", "tier": "composite", "severity": "critical", "aws": "All tiers",
        "infra": True, "icon": "🌋",
        "description": "Turns on latency_spike + dynamodb_throttle + random_500s + queue_backlog + log_flood together.",
        "datadog_signal": "Composite monitor across 4+ signals → P1",
    },
}

COMPOSITE = {"cascading_failure": ["latency_spike", "dynamodb_throttle", "random_500s",
                                   "queue_backlog", "log_flood"]}

CACHE_TTL_S = 3.0
_cache = {"at": 0.0, "doc": None}


class UnhandledFault(Exception):
    """Raised on purpose and left uncaught so Lambda records an Error."""


# ── State ─────────────────────────────────────────────────────────────────
def _load(force=False):
    now = time.time()
    if force or _cache["doc"] is None or now - _cache["at"] > CACHE_TTL_S:
        try:
            doc = get_store().get("CONFIG", "FAULTS", consistent=force) or {}
        except Exception as e:  # never let chaos state reads take the app down
            log.warning("fault state read failed: %s", err_code(e))
            doc = _cache["doc"] or {}
        _cache.update(at=now, doc=doc)
    return _cache["doc"]


def _save(doc):
    doc = dict(doc, PK="CONFIG", SK="FAULTS", updated_at=int(time.time()))
    get_store().put(doc)
    _cache.update(at=time.time(), doc=doc)


def active(name):
    f = _load().get("faults", {}).get(name)
    return bool(f and f.get("until", 0) > time.time())


def status():
    now = time.time()
    out = {}
    for name in CATALOG:
        f = _load().get("faults", {}).get(name)
        on = bool(f and f.get("until", 0) > now)
        out[name] = {"enabled": on,
                     "until": int(f["until"]) if on else None,
                     "by": f.get("by") if on else None}
    return out


def active_count():
    return sum(1 for v in status().values() if v["enabled"])


# ── Infra side effects ────────────────────────────────────────────────────
def _apply_infra(name, enabled, api_id):
    if name == "api_throttling":
        if enabled:
            infra.set_api_throttle(api_id, 1, 2)
        else:
            infra.set_api_throttle(api_id, config.DEFAULT_RATE, config.DEFAULT_BURST)
    elif name == "worker_throttle":
        infra.set_worker_concurrency_zero(enabled)
    elif name == "queue_backlog":
        infra.set_worker_trigger(not enabled)
    elif name == "cold_start_storm":
        infra.recycle_api_function(2500 if enabled else None)


# ── Mutations ─────────────────────────────────────────────────────────────
def toggle(name, enabled, ttl_min=None, user="system", api_id=None):
    if name not in CATALOG:
        raise KeyError(name)
    ttl_min = max(1, min(int(ttl_min or config.FAULT_TTL_DEFAULT_MIN), config.FAULT_TTL_MAX_MIN))
    doc = dict(_load(force=True))
    faults = dict(doc.get("faults", {}))
    if api_id:
        doc["api_id"] = api_id
    api_id = doc.get("api_id")

    members = COMPOSITE.get(name, []) + [name]
    notes = []
    for m in members:
        if CATALOG[m]["infra"]:
            try:
                _apply_infra(m, enabled, api_id)
            except Exception as e:
                # Report it rather than fail: e.g. the account can't reserve concurrency.
                notes.append(f"{m}: {err_code(e)} — {e}")
                log.error("[CHAOS] infra change for %s failed: %s", m, e)
                if enabled:
                    continue
        if enabled:
            faults[m] = {"until": time.time() + ttl_min * 60, "by": user}
        else:
            faults.pop(m, None)
        log.warning("[CHAOS] %s %s by %s (ttl %s min)", m, "ON" if enabled else "OFF", user, ttl_min)

    doc["faults"] = faults
    _save(doc)
    return {"fault": name, "enabled": enabled, "members": members, "ttl_min": ttl_min, "notes": notes}


def reset_all(user="system", api_id=None):
    doc = dict(_load(force=True))
    if api_id:
        doc["api_id"] = api_id
    notes = []
    for name, meta in CATALOG.items():
        if meta["infra"] and name not in COMPOSITE:
            # Always revert infra faults, even if state says off — cheap and idempotent.
            try:
                _apply_infra(name, False, doc.get("api_id"))
            except Exception as e:
                notes.append(f"{name}: {err_code(e)}")
    doc["faults"] = {}
    _save(doc)
    log.warning("[CHAOS] all faults reset by %s", user)
    return {"message": "All faults cleared", "notes": notes}


def janitor():
    """Revert expired faults (called by the scheduled simulator)."""
    doc = dict(_load(force=True))
    faults = dict(doc.get("faults", {}))
    now = time.time()
    expired = [n for n, f in faults.items() if f.get("until", 0) <= now]
    for n in expired:
        if CATALOG.get(n, {}).get("infra") and n not in COMPOSITE:
            try:
                _apply_infra(n, False, doc.get("api_id"))
            except Exception as e:
                log.error("[CHAOS] janitor could not revert %s: %s", n, e)
                continue
        faults.pop(n, None)
        log.warning("[CHAOS] %s expired — auto-reverted", n)
    if expired:
        doc["faults"] = faults
        _save(doc)
    return expired
