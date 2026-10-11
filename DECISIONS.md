# Design decisions

1. **Indexes.** I made `emp_code` unique for employee identity and `(emp_code, date)` unique for one attendance record per employee-day. I added date/employee ordering for attendance lists, employee/punch-in ordering for punch-out lookup, date/status/employee access for analytics, and department/join-date indexes for headcount and trend lookups. I would reject a standalone `late_minutes` index: it has little value without the month date range and would add write/storage cost.

2. **Punch-in race.** Both requests may find the employee and build the same natural key. MongoDB's unique `(emp_code, date)` index allows one insert; the other raises `DuplicateKeyError`, which the API maps to 409. There is no check-then-insert guarantee.

3. **Ties.** The leaderboard uses `$rank`, so tied totals share a rank and the next rank is skipped. It filters on rank after ranking, so every person tied at the limit is returned, ordered by `emp_code`.

4. **Headcount.** The department summary starts from eligible employees and looks up logs. Employees with no matching logs remain in the aggregation and count toward headcount.

5. **100x data.** I would benchmark representative queries, then consider pre-aggregated daily/monthly summaries and archive/retention policies, while retaining source logs for auditability.
