from __future__ import annotations

import sqlite3
from pathlib import Path


database = Path(__file__).resolve().parents[1] / "data" / "job_finder.db"
connection = sqlite3.connect(database)
connection.row_factory = sqlite3.Row

print(f"database={database} size={database.stat().st_size}")
for table in ("runs", "run_companies", "jobs"):
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"{table}: count={count} columns={columns}")

print("runs:")
for row in connection.execute("SELECT * FROM runs ORDER BY run_id"):
    print(dict(row))

print("company results:")
for row in connection.execute(
    "SELECT run_id, company_key, status, jobs_found, new_jobs, title_candidates, "
    "prefilter_rejected, detail_succeeded, detail_failed, llm_succeeded, llm_failed, "
    "review_queue_added, error "
    "FROM run_companies ORDER BY run_id, company_key"
):
    print(dict(row))

print("job counts:")
for row in connection.execute(
    "SELECT company_key, COUNT(*) AS job_count FROM jobs GROUP BY company_key ORDER BY company_key"
):
    print(dict(row))

connection.close()
