# DECISIONS.md

1. **Indexes.** `employees.emp_code` is unique; `attendance_logs(emp_code, date)` is unique and supports record lookup and duplicate punch prevention. Date/sort indexes support attendance listing and monthly late-record scans. `employees(department, joined_on)` and `employees(joined_on)` support department trends and headcount cutoffs. I did not add a standalone status index: the list can use its date/order index and filter status while scanning the requested range.

2. **Punch-in race.** Both requests may pass application validation, but only one insert can satisfy the unique `(emp_code, date)` index. MongoDB accepts one; the other raises `DuplicateKeyError`, which the API returns as 409.

3. **Ties.** The leaderboard uses MongoDB `$rank` over total late minutes, then sorts tied rows by employee code. The limit is applied to rank, not row count, so all employees tied at the cutoff are included and the next rank is skipped.

4. **Headcount.** Department summary starts from eligible employees and looks up monthly logs, so employees without logs still contribute one to headcount. Employees whose join date is after month-end are filtered out.

5. **100x growth.** I would benchmark the pipelines, move long-running analytics to pre-aggregated daily facts, and review indexes against real explain plans before adding more indexes blindly.