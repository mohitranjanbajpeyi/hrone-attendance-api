import unittest
from datetime import date, datetime, timezone

from app.main import (
    EmployeeCreate,
    PunchInRequest,
    attendance_date_for,
    department_trend_pipeline,
    late_leaderboard_pipeline,
    late_minutes,
    overtime_minutes,
    work_hours,
)


class AttendanceRuleTests(unittest.TestCase):
    def test_late_grace_period_and_floor(self):
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc), "09:30", "2026-07-06"), 0)
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc), "09:30", "2026-07-06"), 10)
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc), "09:30", "2026-07-06"), 45)

    def test_work_hours_round_half_up(self):
        start = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(work_hours(start, datetime(2026, 7, 6, 5, 0, 18, tzinfo=timezone.utc)), 1.01)

    def test_overtime_requires_thirty_minutes(self):
        self.assertEqual(overtime_minutes(datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc), "09:30", "18:30", "2026-07-06"), 0)
        self.assertEqual(overtime_minutes(datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc), "09:30", "18:30", "2026-07-06"), 30)

    def test_overnight_date_and_overtime(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        early_punch = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)  # 05:30 IST
        self.assertEqual(attendance_date_for(early_punch, employee).isoformat(), "2026-07-06")
        out = datetime(2026, 7, 7, 1, 0, tzinfo=timezone.utc)  # 06:30 IST
        self.assertEqual(overtime_minutes(out, "22:00", "06:00", "2026-07-06"), 30)

    def test_invalid_timestamp_units_are_rejected(self):
        with self.assertRaises(ValueError):
            PunchInRequest(emp_code="EMP0001", punched_at=1_783_312_500)
        with self.assertRaises(ValueError):
            PunchInRequest(emp_code="EMP0001", punched_at=1_783_312_500_000.0)

    def test_leaderboard_ranks_before_applying_the_limit(self):
        pipeline = late_leaderboard_pipeline("2026-07", None, 2)
        rank_stage = next(stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage)
        self.assertIn("$rank", rank_stage["output"]["rank"])
        rank_filter = next(i for i, stage in enumerate(pipeline) if stage.get("$match", {}).get("rank"))
        window_stage = next(i for i, stage in enumerate(pipeline) if "$setWindowFields" in stage)
        self.assertGreater(rank_filter, window_stage)

    def test_trend_calendar_and_window_are_built_in_mongodb(self):
        pipeline = department_trend_pipeline("Engineering", date(2026, 7, 1), date(2026, 7, 7))
        self.assertTrue(any("$range" in str(stage) for stage in pipeline))
        window = next(stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage)
        self.assertEqual(window["output"]["_moving_avg"]["window"]["documents"], [-6, 0])

    def test_equal_shift_times_are_rejected(self):
        with self.assertRaises(ValueError):
            EmployeeCreate(
                emp_code="EMP0001",
                name="Asha Rao",
                email="asha@example.com",
                department="Engineering",
                shift_start="09:30",
                shift_end="09:30",
                joined_on="2026-01-05",
            )


if __name__ == "__main__":
    unittest.main()