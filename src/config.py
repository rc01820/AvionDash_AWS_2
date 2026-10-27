"""Runtime configuration, read once from the Lambda environment.

Everything has a local default so the same code runs under scripts/local_server.py
(AVIONPULSE_LOCAL=1) with an in-memory data store and simulated AWS calls.
"""
import os

LOCAL = os.environ.get("AVIONPULSE_LOCAL") == "1"

TABLE_NAME = os.environ.get("TABLE_NAME", "avionpulse-local-data")
QUEUE_URL = os.environ.get("QUEUE_URL", "local://events")
DLQ_URL = os.environ.get("DLQ_URL", "local://events-dlq")
BUCKET = os.environ.get("BUCKET", "local-bucket")

API_FN = os.environ.get("API_FN", "avionpulse-api")
WORKER_FN = os.environ.get("WORKER_FN", "avionpulse-worker")
SIM_FN = os.environ.get("SIM_FN", "avionpulse-simulator")

APP_ENV = os.environ.get("APP_ENV", "local")
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
REGION = os.environ.get("AWS_REGION", "local")

# API Gateway default-route throttle set by the template. The api_throttling
# fault drops the stage to 1 rps and restores these values when cleared.
DEFAULT_RATE = float(os.environ.get("DEFAULT_RATE", "20"))
DEFAULT_BURST = int(os.environ.get("DEFAULT_BURST", "40"))

# Every fault auto-reverts after this many minutes unless the operator picks
# another TTL. Keeps a forgotten chaos experiment from running up a bill.
FAULT_TTL_DEFAULT_MIN = int(os.environ.get("FAULT_TTL_MIN", "30"))
FAULT_TTL_MAX_MIN = 120

JWT_FALLBACK_SECRET = os.environ.get("JWT_SECRET", "avionpulse-local-secret")
TOKEN_TTL_S = 8 * 3600

# Local dev only: where the web/ folder lives (for the NOTAM "S3" object).
LOCAL_WEB_ROOT = os.environ.get(
    "LOCAL_WEB_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"),
)
