"""Flight simulator — scheduled by EventBridge (default every 5 minutes).

Each run:
  1. recomputes every flight from the lifecycle model and writes it to DynamoDB
  2. emits business metrics (airborne flights, passengers, on-time %, fleet)
  3. drops a few ops events on the SQS queue for the worker
  4. runs the chaos janitor (auto-reverts expired faults) and the scheduled
     side of async faults (cold-start waves, backlog flood, poison pills, crash)
"""
import logging
import random
import time

import chaos
import config
import flightsim
import infra
import telemetry
from seed_data import AIRCRAFT, AIRPORTS, OPS_EVENT_TEMPLATES
from store import get_store

telemetry.setup_logging()
log = logging.getLogger("avionpulse.simulator")


class SimulatorCrash(Exception):
    pass


def _ops_event(rng):
    sev, cat, tmpl = rng.choice(OPS_EVENT_TEMPLATES)
    tail = rng.choice(AIRCRAFT)[0]
    apt = rng.choice(AIRPORTS)[0]
    return {"type": "ops_event", "severity": sev, "category": cat, "tail_number": tail, "airport": apt,
            "message": tmpl.format(tail=tail, apt=apt), "enqueued_at": time.time(), "source": "simulator"}


def run(now=None):
    now = now or time.time()
    store = get_store()

    expired = chaos.janitor()
    if chaos.active("cold_start_storm"):
        try:
            infra.recycle_api_function(2500)
            log.warning("[CHAOS] cold_start_storm: recycled API execution environments")
        except Exception as e:
            log.error("cold_start_storm recycle failed: %s", e)

    flights = flightsim.all_flights(now)
    if chaos.active("simulator_crash"):
        store.put_many(flights[: len(flights) // 2])
        raise SimulatorCrash("KeyError: 'arrival_slot' — flight state writer crashed mid-batch (injected)")
    store.put_many(flights)

    # Business metrics are computed from what the data tier returns, so a
    # silent data bug shows up here even though nothing errors.
    pk = "FLIGHTS" if chaos.active("silent_failure") else "FLIGHT"
    stored = store.query_pk(pk)
    pax_mult = 847 if chaos.active("pax_corruption") else 1
    airborne = [f for f in stored if f.get("status") in ("en_route", "departed")]
    delayed = [f for f in stored if f.get("delay_minutes", 0) > 0]
    cancelled = [f for f in stored if f.get("status") == "cancelled"]
    total = len(stored)
    telemetry.metric("avionpulse.flights.airborne", len(airborne))
    telemetry.metric("avionpulse.flights.delayed", len(delayed))
    telemetry.metric("avionpulse.flights.cancelled", len(cancelled))
    telemetry.metric("avionpulse.flights.total", total)
    telemetry.metric("avionpulse.flights.on_time_pct",
                     round((total - len(delayed) - len(cancelled)) / max(total, 1) * 100, 1))
    telemetry.metric("avionpulse.pax.airborne", sum(int(f.get("pax", 0)) for f in airborne) * pax_mult)
    telemetry.metric("avionpulse.fleet.in_maintenance", sum(1 for a in AIRCRAFT if a[5] == "maintenance"))

    rng = random.Random()
    events = [_ops_event(rng) for _ in range(rng.randint(1, 3))]
    if chaos.active("queue_backlog"):
        events += [_ops_event(rng) for _ in range(20)]
    for e in events:
        infra.send_event(e)
    if chaos.active("sqs_poison_pill"):
        for _ in range(5):
            infra.send_event({"type": "poison", "payload": "\x00\x7f{not-json", "source": "simulator"})

    summary = {"flights_written": len(flights), "airborne": len(airborne), "events_enqueued": len(events),
               "faults_expired": expired, "active_faults": chaos.active_count()}
    log.info("simulator run complete", extra=summary)
    return summary


def handler(event, context):
    return run()


if __name__ == "__main__" and config.LOCAL:
    print(run())
