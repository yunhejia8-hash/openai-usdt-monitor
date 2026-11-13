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

    def test_one_missed_schedule_window_is_degraded(self):
        now = datetime(2026, 9, 28, 6, 40, tzinfo=timezone.utc)
        latest = {"generated_at_utc": now.isoformat()}
        previous = {"last_schedule_observed_at_utc": (now - timedelta(minutes=35)).isoformat()}
        h = workflow_health.build_health(
            latest, previous, now=now, event="workflow_dispatch", trigger_source="watchdog",
            run_id="x", run_attempt="1", trigger_sha="abc", previous_age_minutes=35,
        )
        self.assertTrue(h["schedule_degraded"])
        self.assertEqual(h["missed_schedule_windows"], 1)
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
        self.assertIsNotNone(h["snapshot_due_at_utc"])
        self.assertIsNotNone(h["snapshot_fresh_until_utc"])

    def test_watchdog_dispatch_threshold_and_dedupe(self):
        self.assertTrue(watchdog.should_dispatch(45, 0))
        self.assertFalse(watchdog.should_dispatch(44.9, 0))
        self.assertFalse(watchdog.should_dispatch(90, 1))
        self.assertTrue(watchdog.should_dispatch(1, 0, force=True))
        self.assertFalse(watchdog.should_dispatch(1, 1, force=True))


class TestWatchdogParseTimestamp(unittest.TestCase):
    def test_accepts_utc_z_suffix_at_day_boundary(self):
        parsed = watchdog.parse_ts("2026-01-01T00:00:00Z")

        self.assertEqual(parsed, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_preserves_explicit_offset_and_naive_values(self):
        self.assertEqual(
            watchdog.parse_ts("2026-01-01T00:00:00+05:30"),
            datetime.fromisoformat("2026-01-01T00:00:00+05:30"),
        )
        self.assertEqual(
            watchdog.parse_ts("2026-01-01T00:00:00"),
            datetime(2026, 1, 1),
        )

    def test_rejects_malformed_and_non_string_values(self):
        with self.assertRaises(ValueError):
            watchdog.parse_ts("not-a-timestamp")
        with self.assertRaises(AttributeError):
            watchdog.parse_ts(None)


if __name__ == "__main__":
    unittest.main()
