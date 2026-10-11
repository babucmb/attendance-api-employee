# Starter code review

Findings below refer to the original starter `app/main.py`. The corrections are
implemented in the current version. The numbered table contains 16 genuine
starter defects; assignment features that were not present in the starter are
listed separately below, not counted as defects.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Replaces the punch timestamp's clock fields without converting the instant to IST; the result depends on whether the input is UTC, naive, or local time. It also cannot account for an overnight shift's previous-day start. | Use an aware UTC punch corresponding to 10:05 IST: it should be 35 minutes late for a 09:30 shift. Test an early-morning overnight punch against the previous day's 22:00 start. | Normalize instants to UTC, calculate in IST, and pass shift end to select the previous shift day when appropriate. |
| 2 | `compute_late_minutes` | The threshold check uses floored minutes (`minutes > 10`), incorrectly giving zero for any punch between 10:00:01 and 10:10:59 after shift start. | For a 09:30 shift, 09:40:01 must yield 10 late minutes, not zero. | Compare elapsed whole seconds to the strict 10-minute threshold, then floor elapsed minutes. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even behavior rather than the required decimal half-up rounding, and calculation is performed on untruncated input instants. | A duration of exactly 18 seconds is 0.005 hours and must round to 0.01; fractional-second instants must be truncated first. | Truncate to whole seconds and use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It omits the 30-minute minimum and always reports any positive overtime. It also puts an overnight shift end on the wrong calendar day. | 18:59:59 for an 18:30 end must report 0; a 06:40 punch-out for a 22:00-06:00 shift must report 40. | Apply the 30-minute threshold and calculate overnight shift end on the next day in IST. |
| 5 | MongoDB setup / indexes | No application indexes are created, so uniqueness and expected query performance are not guaranteed. | Concurrent creates or punches can create duplicate natural keys; list/analytics scans grow with the collection. | Create idempotent unique employee and attendance-key indexes, plus indexes for list, punch-out, and analytics access. |
| 6 | `health` | Always returns `{"status":"ok"}` without checking MongoDB, so an unavailable database still appears ready. | Stop MongoDB and call `/health`; the starter still returns 200. | Ping MongoDB and return 503 if the database cannot be reached. |
| 7 | `create_employee` | A pre-insert existence check is a race; two simultaneous requests can both pass it. It also stores a naive `created_at` and returns a Mongo `_id`-stripped document without converting `created_at` to epoch milliseconds. | Send concurrent requests with the same `emp_code`; inspect `created_at` response type. | Rely on a unique index, map duplicate-key errors to 409, store UTC time, and serialize it as epoch milliseconds. |
| 8 | `EmployeeIn` | The model does not enforce contract formats, length limits, real calendar dates, or distinct shift start/end. | Try a malformed employee code, invalid date, malformed shift, or identical shift times. | Add Pydantic validation for each contract field and the cross-field shift/date rules. |
| 9 | `list_employees` | Page 1 skips `page_size` records; filtered results are counted using an unfiltered count; results are unsorted; page values are not constrained. | With 25 employees and page size 20, page 1 starts at the 21st result and the filtered total reports 25 for any department. | Use `(page - 1) * page_size`, count the filtered query, sort by `emp_code`, and validate page bounds. |
| 10 | `punch_in` | It dereferences an unknown employee, interprets epoch milliseconds as seconds, relies on the machine's local timezone, and derives the date without the IST/overnight rule. | Punch in an unknown code or pass a valid epoch-millisecond instant; the starter may return 500 or calculate a wrong date/time. | Return 404 for an unknown employee; validate and convert epoch milliseconds as UTC; derive the IST attendance date. |
| 11 | `PunchInIn` / `punch_in` | Status values and epoch-millisecond types/ranges are not validated; seconds, floats, strings, and invalid statuses can reach business logic. | Try seconds instead of milliseconds or a status such as `ABSENT`; the starter accepts or misinterprets them. | Use strict bounded integer timestamps and a presence-only status literal. |
| 12 | `punch_in` | A check-then-insert duplicate lookup does not prevent simultaneous punch-ins from creating the same `(emp_code, date)` record. | Send concurrent identical punch-ins and count records/results. | Enforce a unique compound index and translate duplicate-key failure to 409. |
| 13 | `punch_in` response | Adds an `id` from MongoDB's internal ObjectId, although the contract says attendance records are addressed by `(emp_code, date)` and expose no id. | Compare the response keys with the OpenAPI `AttendanceRecord` schema. | Return only contract fields; keep `_id` internal. |
| 14 | `list_attendance` | Reads every matching document into Python, sorts and paginates in memory, so it does not scale to the stated collection size. | Inspect query behavior with a large dataset; memory and latency increase with all matches rather than page size. | Use MongoDB count, sort, skip, and limit operations. |
| 15 | `list_attendance` | Sorts only by date, omitting the required ascending `emp_code` tie-breaker; date bounds are not validated and are not parsed as calendar dates. | Add multiple records on the same date or pass `date_from > date_to`. | Validate date parameters and sort by date descending, then employee code ascending. |
| 16 | Attendance responses | Stored BSON dates are returned directly, despite the contract requiring epoch milliseconds—including nested history timestamps and changed punch times. Legacy missing fields also are not normalized. | Request a record with punch times/history and inspect JSON types; test a record missing `history` or `half_day`. | Add a response serializer for top-level and nested instants and apply documented defaults. |

## Looked at and not considered a defect

- The employee business key is `emp_code`, not MongoDB `_id`; using the code in
  attendance records matches the provided data model.
- A missing attendance record is not the same as an `ABSENT` record. The API
  does not need to invent absent records for unlogged days.
- The sample documents intentionally omit `history` and `half_day` in a legacy
  example; the contract requires treating those as `[]` and `false`, not
  rejecting the documents.

## Assignment implementation scope (not starter defects)

The starter did not include punch-out, regularization, analytics, or explain
endpoints. The assignment explicitly asks the candidate to implement these
features, so their absence is scope to complete rather than a defect in the
starter. The current application implements them in `app/main.py`; the analytics
use MongoDB aggregation pipelines and the explain endpoint reports execution
plans for the supported operations.
