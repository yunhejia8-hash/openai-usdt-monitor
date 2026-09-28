import unittest
from datetime import datetime, timedelta, timezone

import watchdog
import workflow_health


class WorkflowHealthTests(unittest.TestCase):
    def test_push_does_not_reset_schedule_health(self):
        now = datetime(2026, 9, 28, 6, 40, tzinfo=timezone.utc)
        latest = {"generated_at_utc": now.isoformat()}
        previous = {
            "event": "schedule",
            "updated_at_utc": (now - timedelta(minutes=95)).isoformat(),
            "last_schedule_observed_at_utc": (now - timedelta(minutes=95)).isoformat(),
        }
        h = workflow_health.build_health(
            latest, previous, now=now, event="push", trigger_source="push",
            run_id="1", run_attempt="1", trigger_sha="abc", previous_age_minutes=5,
        )
        self.assertTrue(h["schedule_degraded"])
        self.assertGreaterEqual(h["missed_schedule_windows"], 2)
        self.assertEqual(h["health_status"], "schedule_degraded")

    def test_schedule_run_recovers_gap_but_records_it(self):
        now = datetime(2026, 9, 28, 6, 40, tzinfo=timezone.utc)
        latest = {"generated_at_utc": now.isoformat()}
        previous = {"last_schedule_observed_at_utc": (now - timedelta(minutes=125)).isoformat()}
        h = workflow_health.build_health(
            latest, previous, now=now, event="schedule", trigger_source="schedule",
            run_id="2", run_attempt="1", trigger_sha="def", previous_age_minutes=125,
        )
        self.assertFalse(h["schedule_degraded"])
        self.assertGreater(h["previous_schedule_gap_minutes"], 120)
        self.assertEqual(h["health_status"], "recovered")

    def test_watchdog_dispatch_threshold_and_dedupe(self):
        self.assertTrue(watchdog.should_dispatch(55, 0))
        self.assertFalse(watchdog.should_dispatch(49, 0))
        self.assertFalse(watchdog.should_dispatch(90, 1))
        self.assertTrue(watchdog.should_dispatch(1, 0, force=True))
        self.assertFalse(watchdog.should_dispatch(1, 1, force=True))


if __name__ == "__main__":
    unittest.main()
