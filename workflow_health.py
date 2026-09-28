import json
import os
from datetime import datetime, timezone
from pathlib import Path

LATEST_PATH = Path("output/latest.json")
HEALTH_PATH = Path("output/workflow_health.json")
TARGET_INTERVAL_MINUTES = 30
FRESHNESS_SLA_MINUTES = 60


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def build_health(latest, previous, *, now, event, trigger_source, run_id, run_attempt, trigger_sha, previous_age_minutes=None):
    generated = latest.get("generated_at_utc")
    generated_dt = parse_ts(generated)
    snapshot_age = (now - generated_dt).total_seconds() / 60 if generated_dt else None

    prior_schedule = previous.get("last_schedule_observed_at_utc")
    if not prior_schedule and previous.get("event") == "schedule":
        prior_schedule = previous.get("updated_at_utc")

    prior_schedule_dt = parse_ts(prior_schedule)
    gap_before_current_schedule = None
    if event == "schedule" and prior_schedule_dt:
        gap_before_current_schedule = (now - prior_schedule_dt).total_seconds() / 60

    last_schedule = now.isoformat() if event == "schedule" else prior_schedule
    last_schedule_dt = parse_ts(last_schedule)
    schedule_age = (now - last_schedule_dt).total_seconds() / 60 if last_schedule_dt else None

    # Two consecutive target windows without a schedule observation is degraded.
    missed_windows = 0 if schedule_age is None else max(0, int(schedule_age // TARGET_INTERVAL_MINUTES) - 1)
    recovered_schedule_gap = bool(gap_before_current_schedule and gap_before_current_schedule > FRESHNESS_SLA_MINUTES)
    schedule_degraded = (schedule_age is None) or missed_windows >= 2

    prior_age = previous_age_minutes
    stale_gap_recovered = prior_age is not None and prior_age > FRESHNESS_SLA_MINUTES

    if snapshot_age is None or snapshot_age > FRESHNESS_SLA_MINUTES:
        status = "snapshot_stale"
    elif schedule_degraded:
        status = "schedule_degraded"
    elif recovered_schedule_gap or stale_gap_recovered or trigger_source == "watchdog":
        status = "recovered"
    else:
        status = "healthy"

    return {
        "schema_version": 3,
        "updated_at_utc": now.isoformat(),
        "event": event,
        "trigger_source": trigger_source,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "trigger_sha": trigger_sha,
        "snapshot_generated": bool(generated),
        "snapshot_generated_at_utc": generated,
        "snapshot_age_minutes_at_health_write": round(snapshot_age, 3) if snapshot_age is not None else None,
        "previous_snapshot_age_minutes": round(prior_age, 3) if prior_age is not None else None,
        "freshness_sla_minutes": FRESHNESS_SLA_MINUTES,
        "target_snapshot_interval_minutes": TARGET_INTERVAL_MINUTES,
        "last_schedule_observed_at_utc": last_schedule,
        "schedule_age_minutes": round(schedule_age, 3) if schedule_age is not None else None,
        "previous_schedule_gap_minutes": round(gap_before_current_schedule, 3) if gap_before_current_schedule is not None else None,
        "missed_schedule_windows": missed_windows,
        "schedule_degraded": schedule_degraded,
        "health_status": status,
    }


def main():
    latest = load_json(LATEST_PATH)
    previous = load_json(HEALTH_PATH)
    previous_age = os.getenv("PREVIOUS_AGE_MINUTES")
    health = build_health(
        latest,
        previous,
        now=datetime.now(timezone.utc),
        event=os.getenv("RUN_EVENT", "unknown"),
        trigger_source=os.getenv("TRIGGER_SOURCE") or os.getenv("RUN_EVENT", "unknown"),
        run_id=os.getenv("RUN_ID", ""),
        run_attempt=os.getenv("RUN_ATTEMPT", ""),
        trigger_sha=os.getenv("RUN_SHA", ""),
        previous_age_minutes=float(previous_age) if previous_age else None,
    )
    HEALTH_PATH.write_text(json.dumps(health, indent=2), encoding="utf-8")
    print(json.dumps(health, indent=2))


if __name__ == "__main__":
    main()
