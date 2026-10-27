"""Custom metrics and logging.

When the Datadog Lambda layer is attached, metrics go out through the Datadog
Lambda Extension (no CloudWatch custom-metric charges). Without it, each metric
is written as a structured log line so a Datadog log-based metric (or a quick
CloudWatch Logs Insights query) can still pick it up.
"""
import logging
import os
import sys

import config

_lambda_metric = None
_resolved = False


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if config.LOCAL and not root.handlers:
        logging.basicConfig(
            stream=sys.stdout,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    # boto noise isn't useful in a demo
    for noisy in ("botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _resolve():
    global _lambda_metric, _resolved
    if not _resolved:
        _resolved = True
        if not config.LOCAL and os.environ.get("DD_LAMBDA_HANDLER"):
            try:
                from datadog_lambda.metric import lambda_metric

                _lambda_metric = lambda_metric
            except ImportError:
                _lambda_metric = None
    return _lambda_metric


_mlog = logging.getLogger("avionpulse.metric")


def metric(name, value, tags=None):
    tags = list(tags or []) + [f"env:{config.APP_ENV}"]
    fn = _resolve()
    if fn:
        fn(name, value, tags=tags)
    else:
        _mlog.info("metric %s=%s %s", name, value, ",".join(tags),
                   extra={"metric_name": name, "metric_value": value, "metric_tags": tags})
