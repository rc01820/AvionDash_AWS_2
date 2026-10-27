"""Calls to AWS services other than DynamoDB: SQS, S3, Lambda, API Gateway.

The chaos engine uses the control-plane calls here (stage throttling, reserved
concurrency, event source mappings, env-var updates) to break real AWS
resources and put them back. In local mode every call is simulated and logged.
"""
import json
import logging
import os
import threading
import time
import uuid

import config
from store import SimulatedAwsError

log = logging.getLogger("avionpulse.infra")

_clients = {}
_clients_lock = threading.Lock()


def _client(name):
    if name not in _clients:
        with _clients_lock:
            if name not in _clients:
                import boto3
                from botocore.config import Config

                _clients[name] = boto3.client(
                    name,
                    config=Config(retries={"mode": "standard", "max_attempts": 3},
                                  connect_timeout=3, read_timeout=5),
                )
    return _clients[name]


# ── Local simulation state ────────────────────────────────────────────────
LOCAL_QUEUE = []          # messages waiting for the worker
LOCAL_DLQ = []
LOCAL_SIM = {             # pretend control-plane state
    "api_throttle": None,
    "worker_concurrency": None,
    "esm_enabled": True,
    "api_env": {},
}
_local_lock = threading.Lock()


# ── SQS ───────────────────────────────────────────────────────────────────
def send_event(message):
    body = json.dumps(message, default=str)
    if config.LOCAL:
        with _local_lock:
            LOCAL_QUEUE.append({"messageId": str(uuid.uuid4()), "body": body,
                                "receiveCount": 0, "sent": time.time()})
        return
    _client("sqs").send_message(QueueUrl=config.QUEUE_URL, MessageBody=body)


def queue_depths():
    if config.LOCAL:
        with _local_lock:
            oldest = min((m["sent"] for m in LOCAL_QUEUE), default=None)
            return {
                "queue_visible": len(LOCAL_QUEUE),
                "queue_in_flight": 0,
                "queue_oldest_age_s": int(time.time() - oldest) if oldest else 0,
                "dlq_visible": len(LOCAL_DLQ),
            }
    sqs = _client("sqs")
    attrs = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    q = sqs.get_queue_attributes(QueueUrl=config.QUEUE_URL, AttributeNames=attrs)["Attributes"]
    d = sqs.get_queue_attributes(QueueUrl=config.DLQ_URL, AttributeNames=attrs[:1])["Attributes"]
    return {
        "queue_visible": int(q.get("ApproximateNumberOfMessages", 0)),
        "queue_in_flight": int(q.get("ApproximateNumberOfMessagesNotVisible", 0)),
        "queue_oldest_age_s": None,  # CloudWatch-only metric (ApproximateAgeOfOldestMessage)
        "dlq_visible": int(d.get("ApproximateNumberOfMessages", 0)),
    }


# ── S3 ────────────────────────────────────────────────────────────────────
def get_s3_json(key):
    if config.LOCAL:
        path = os.path.join(config.LOCAL_WEB_ROOT, *key.split("/"))
        if not os.path.exists(path):
            raise SimulatedAwsError("NoSuchKey", f"The specified key does not exist: {key}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    obj = _client("s3").get_object(Bucket=config.BUCKET, Key=key)
    return json.loads(obj["Body"].read())


# ── API Gateway (HTTP API stage throttling) ───────────────────────────────
def set_api_throttle(api_id, rate, burst):
    if config.LOCAL:
        LOCAL_SIM["api_throttle"] = (rate, burst)
        log.warning("[LOCAL] apigatewayv2.update_stage throttle rate=%s burst=%s", rate, burst)
        return
    if not api_id:
        raise RuntimeError("API id unknown — toggle this fault from the web UI")
    _client("apigatewayv2").update_stage(
        ApiId=api_id,
        StageName="$default",
        DefaultRouteSettings={"ThrottlingRateLimit": rate, "ThrottlingBurstLimit": burst},
    )


# ── Lambda: reserved concurrency on the SQS worker ────────────────────────
def set_worker_concurrency_zero(enabled):
    if config.LOCAL:
        LOCAL_SIM["worker_concurrency"] = 0 if enabled else None
        log.warning("[LOCAL] lambda.%s_function_concurrency %s",
                    "put" if enabled else "delete", config.WORKER_FN)
        return
    lam = _client("lambda")
    if enabled:
        lam.put_function_concurrency(FunctionName=config.WORKER_FN, ReservedConcurrentExecutions=0)
    else:
        lam.delete_function_concurrency(FunctionName=config.WORKER_FN)


# ── Lambda: pause/resume the SQS -> worker event source mapping ───────────
def set_worker_trigger(enabled):
    if config.LOCAL:
        LOCAL_SIM["esm_enabled"] = enabled
        log.warning("[LOCAL] lambda.update_event_source_mapping Enabled=%s", enabled)
        return
    lam = _client("lambda")
    for m in lam.list_event_source_mappings(FunctionName=config.WORKER_FN)["EventSourceMappings"]:
        lam.update_event_source_mapping(UUID=m["UUID"], Enabled=enabled)


def worker_paused():
    """True when queue_backlog or worker_throttle should stop local draining."""
    return config.LOCAL and (not LOCAL_SIM["esm_enabled"] or LOCAL_SIM["worker_concurrency"] == 0)


# ── Lambda: recycle the API function's execution environments ─────────────
def recycle_api_function(slow_init_ms):
    """Touching an env var forces every warm container to be replaced, so the
    next requests all cold-start. slow_init_ms adds a sleep during init."""
    if config.LOCAL:
        env = LOCAL_SIM["api_env"]
        env["CHAOS_NONCE"] = uuid.uuid4().hex[:8]
        if slow_init_ms:
            env["CHAOS_SLOW_INIT_MS"] = str(slow_init_ms)
        else:
            env.pop("CHAOS_SLOW_INIT_MS", None)
        log.warning("[LOCAL] lambda.update_function_configuration %s env=%s", config.API_FN, env)
        return
    lam = _client("lambda")
    for attempt in range(3):
        cur = lam.get_function_configuration(FunctionName=config.API_FN)
        env = dict(cur.get("Environment", {}).get("Variables", {}))
        env["CHAOS_NONCE"] = uuid.uuid4().hex[:8]
        if slow_init_ms:
            env["CHAOS_SLOW_INIT_MS"] = str(slow_init_ms)
        else:
            env.pop("CHAOS_SLOW_INIT_MS", None)
        try:
            lam.update_function_configuration(FunctionName=config.API_FN,
                                              Environment={"Variables": env})
            return
        except lam.exceptions.ResourceConflictException:
            # A previous update is still rolling out.
            time.sleep(2 + attempt * 2)
    raise RuntimeError("Lambda configuration update still in progress — try again shortly")


# ── Downstream dependency (downstream_timeout fault) ──────────────────────
def call_weather_provider():
    """10.255.255.1 is unroutable from a non-VPC Lambda: the connect hangs
    until the timeout fires, exactly like a dead third-party API."""
    import urllib.request

    urllib.request.urlopen("http://10.255.255.1:81/v1/metar", timeout=3)
