# DECISIONS.md

1. **Indexes.**
We created unique indexes on `employees.emp_code` and `attendance_logs(emp_code, date)` to enforce uniqueness and eliminate punch-in race conditions. We added `attendance_logs(date: -1, emp_code: 1)` to provide index-covered sorting for listing (`IXSCAN`), `employees(department, joined_on)` for department filtering, and `attendance_logs(date, late_minutes)` for leaderboard queries. We considered and rejected an index on `punch_out` alone because attendance queries filter primarily on `emp_code` and `date`, where compound indexes already provide optimal selectivity.

2. **Punch-in race.**
Both concurrent requests validate the employee and attempt to insert `(emp_code, date)`. MongoDB's unique compound index atomically accepts the first insert and rejects the second with a `DuplicateKeyError`. Our code catches this error and returns HTTP 409, ensuring exactly one 201 response and no duplicate records.

3. **Ties.**
We calculate standard competition ranking using MongoDB's `$rank` window function (e.g. 1, 2, 2, 4) with ties ordered secondarily by `emp_code` ascending. Filtering `rank <= limit` is applied *after* ranking, so all employees tied at the cutoff are included, even if the count exceeds `limit`.

4. **Headcount.**
The department summary aggregation originates from `employees` filtered by `joined_on <= period_end`. Department headcount is calculated from the employee collection before `$lookup`-ing `attendance_logs`, ensuring employees with zero logs are fully counted.

5. **One thing you would change for 100x data.**
Maintain pre-aggregated daily attendance summaries updated incrementally at punch-out and regularization, avoiding expensive multi-collection table scans across millions of raw logs during monthly reporting.
