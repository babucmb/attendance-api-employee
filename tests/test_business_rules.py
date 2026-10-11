import unittest
from datetime import date, datetime

from pydantic import ValidationError

from app.main import (
    EmployeeIn,
    IST,
    PunchInIn,
    attendance_date_for,
    attendance_list_filter,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    datetime_to_epoch_millis,
    epoch_millis_to_datetime,
    month_bounds,
)


class TimeRuleTests(unittest.TestCase):
    def test_late_grace_boundary_is_strict_and_floors_minutes(self):
        self.assertEqual(compute_late_minutes(datetime(2026, 7, 6, 9, 40, tzinfo=IST), "09:30"), 0)
        self.assertEqual(compute_late_minutes(datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST), "09:30"), 10)
        self.assertEqual(compute_late_minutes(datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST), "09:30"), 45)

    def test_overnight_punch_uses_previous_shift_day(self):
        punched_at = datetime(2026, 7, 7, 2, 0, tzinfo=IST)
        self.assertEqual(attendance_date_for(punched_at, "22:00", "06:00"), date(2026, 7, 6))
        self.assertEqual(compute_late_minutes(punched_at, "22:00", "06:00"), 240)

    def test_work_hours_use_half_up_rounding_after_second_truncation(self):
        start = datetime(2026, 7, 6, 9, 0, 0, 900000, tzinfo=IST)
        below_half = datetime(2026, 7, 6, 9, 0, 17, 900000, tzinfo=IST)
        at_half = datetime(2026, 7, 6, 9, 0, 18, 900000, tzinfo=IST)
        self.assertEqual(compute_work_hours(start, below_half), 0.0)
        self.assertEqual(compute_work_hours(start, at_half), 0.01)

    def test_overtime_requires_thirty_full_minutes(self):
        self.assertEqual(
            compute_overtime(datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST), "18:30", "2026-07-06", "09:30"),
            0,
        )
        self.assertEqual(
            compute_overtime(datetime(2026, 7, 6, 19, 0, tzinfo=IST), "18:30", "2026-07-06", "09:30"),
            30,
        )

    def test_epoch_round_trip_discards_fractional_seconds(self):
        value = epoch_millis_to_datetime(1783312500123)
        self.assertEqual(datetime_to_epoch_millis(value), 1783312500000)


class RequestValidationTests(unittest.TestCase):
    def test_punch_timestamp_must_be_integer_milliseconds_in_range(self):
        for invalid in (1783312500, 1783312500000.0, "1783312500000", True, 99999999999, 4102444800001):
            with self.subTest(value=invalid), self.assertRaises(ValidationError):
                PunchInIn(emp_code="EMP1234", punched_at=invalid)

    def test_employee_date_and_shift_must_be_valid(self):
        valid = {
            "emp_code": "EMP1234",
            "name": "Test Employee",
            "email": "test@example.com",
            "department": "Engineering",
            "joined_on": "2026-02-28",
        }
        EmployeeIn(**valid)
        for invalid in (
            {**valid, "joined_on": "2026-02-30"},
            {**valid, "shift_start": "09:30", "shift_end": "09:30"},
        ):
            with self.subTest(payload=invalid), self.assertRaises(ValidationError):
                EmployeeIn(**invalid)


class QueryConstructionTests(unittest.TestCase):
    def test_attendance_filter_is_inclusive_and_preserves_empty_values(self):
        query = attendance_list_filter(
            "EMP1234",
            date(2026, 7, 1),
            date(2026, 7, 31),
            "PRESENT",
        )
        self.assertEqual(
            query,
            {
                "emp_code": "EMP1234",
                "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"},
                "status": "PRESENT",
            },
        )

    def test_month_bounds_handles_december(self):
        self.assertEqual(
            month_bounds("2026-12"),
            ("2026-12-01", "2027-01-01", date(2026, 12, 31)),
        )


if __name__ == "__main__":
    unittest.main()
