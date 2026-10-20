from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from connectors import JobPosting


PROCESSING_STATUSES = {
    "discovered",
    "prefilter_rejected",
    "detail_pending",
    "detail_failed",
    "llm_pending",
    "llm_failed",
    "review_queue",
}
RETRYABLE_PROCESSING_STATUSES = {
    "discovered",
    "detail_pending",
    "detail_failed",
    "llm_pending",
    "llm_failed",
}
TERMINAL_PROCESSING_STATUSES = {"prefilter_rejected", "review_queue"}
ALLOWED_PROCESSING_TRANSITIONS = {
    "discovered": {"prefilter_rejected", "detail_pending"},
    "prefilter_rejected": set(),
    "detail_pending": {"prefilter_rejected", "detail_failed", "llm_pending"},
    "detail_failed": {"prefilter_rejected", "detail_pending"},
    "llm_pending": {"llm_failed", "review_queue"},
    "llm_failed": {"llm_pending"},
    "review_queue": set(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_signature(company_key: str, source_job_id: str) -> str:
    raw = f"{company_key}|{source_job_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS companies (
                company_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                connector TEXT NOT NULL,
                connector_key TEXT NOT NULL,
                careers_url TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                initialized_at TEXT,
                last_success_at TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running'
            );
            CREATE TABLE IF NOT EXISTS run_companies (
                run_id INTEGER NOT NULL,
                company_key TEXT NOT NULL,
                status TEXT NOT NULL,
                jobs_found INTEGER NOT NULL DEFAULT 0,
                new_jobs INTEGER NOT NULL DEFAULT 0,
                title_candidates INTEGER NOT NULL DEFAULT 0,
                prefilter_rejected INTEGER NOT NULL DEFAULT 0,
                detail_succeeded INTEGER NOT NULL DEFAULT 0,
                detail_failed INTEGER NOT NULL DEFAULT 0,
                llm_succeeded INTEGER NOT NULL DEFAULT 0,
                llm_failed INTEGER NOT NULL DEFAULT 0,
                review_queue_added INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, company_key)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                company_key TEXT NOT NULL,
                signature TEXT NOT NULL,
                source_job_id TEXT NOT NULL,
                title TEXT NOT NULL,
                location TEXT NOT NULL,
                job_url TEXT NOT NULL,
                description_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                first_run_id INTEGER NOT NULL,
                last_run_id INTEGER NOT NULL,
                baseline INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (company_key, signature)
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_source_id ON jobs(company_key, source_job_id);
            CREATE TABLE IF NOT EXISTS job_processing (
                company_key TEXT NOT NULL,
                signature TEXT NOT NULL,
                status TEXT NOT NULL,
                filter_reason TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                description_hash TEXT NOT NULL DEFAULT '',
                detail_attempts INTEGER NOT NULL DEFAULT 0,
                llm_attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                evaluation_json TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY (company_key, signature),
                FOREIGN KEY (company_key, signature)
                    REFERENCES jobs(company_key, signature) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_job_processing_status
                ON job_processing(company_key, status);
            """
        )
        existing_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(run_companies)")
        }
        for column in (
            "prefilter_rejected",
            "detail_succeeded",
            "detail_failed",
            "llm_succeeded",
            "llm_failed",
            "review_queue_added",
        ):
            if column not in existing_columns:
                self.connection.execute(
                    f"ALTER TABLE run_companies ADD COLUMN {column} "
                    "INTEGER NOT NULL DEFAULT 0"
                )
        self.connection.commit()

    def start_run(self, mode: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(started_at, mode) VALUES (?, ?)", (utc_now(), mode)
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        self.connection.execute(
            "UPDATE runs SET completed_at = ?, status = ? WHERE run_id = ?",
            (utc_now(), status, run_id),
        )
        self.connection.commit()

    def sync_companies(self, companies: list[dict]) -> None:
        active_keys = {company["company_key"] for company in companies}
        self.connection.execute("UPDATE companies SET active = 0")
        for company in companies:
            self._upsert_company(company, commit=False)
        if not active_keys:
            self.connection.execute("UPDATE companies SET active = 0")
        self.connection.commit()

    def _upsert_company(self, company: dict, *, commit: bool) -> None:
        self.connection.execute(
            """
            INSERT INTO companies(company_key, display_name, connector, connector_key, careers_url, active)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(company_key) DO UPDATE SET
                display_name=excluded.display_name,
                connector=excluded.connector,
                connector_key=excluded.connector_key,
                careers_url=excluded.careers_url,
                active=1
            """,
            (
                company["company_key"],
                company["company"],
                company.get("connector", ""),
                company.get("connector_key", ""),
                company.get("careers_url", ""),
            ),
        )
        if commit:
            self.connection.commit()

    def upsert_company(self, company: dict) -> None:
        """Add or update one company without changing the active state of other companies."""
        self._upsert_company(company, commit=True)

    def is_initialized(self, company_key: str) -> bool:
        row = self.connection.execute(
            "SELECT initialized_at FROM companies WHERE company_key = ?", (company_key,)
        ).fetchone()
        return bool(row and row["initialized_at"])

    def get_company(self, company_key: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM companies WHERE company_key = ?", (company_key,)
        ).fetchone()
        return dict(row) if row else None

    def company_keys(self) -> set[str]:
        return {
            str(row["company_key"])
            for row in self.connection.execute("SELECT company_key FROM companies")
        }

    def find_company_by_display_name(self, display_name: str) -> dict | None:
        rows = self.connection.execute(
            "SELECT * FROM companies WHERE lower(display_name) = lower(?)",
            (display_name.strip(),),
        ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def store_postings(
        self,
        company_key: str,
        postings: list[JobPosting],
        run_id: int,
        *,
        baseline: bool,
    ) -> list[JobPosting]:
        now = utc_now()
        new_postings: list[JobPosting] = []
        for posting in postings:
            signature = make_signature(company_key, posting.source_job_id)
            existing = self.connection.execute(
                "SELECT 1 FROM jobs WHERE company_key = ? AND signature = ?",
                (company_key, signature),
            ).fetchone()
            description_hash = hashlib.sha256(posting.description.encode("utf-8")).hexdigest()
            if existing:
                self.connection.execute(
                    """
                    UPDATE jobs SET title=?, location=?, job_url=?, description_hash=?,
                        last_seen_at=?, last_run_id=?, raw_json=?
                    WHERE company_key=? AND signature=?
                    """,
                    (
                        posting.title,
                        posting.location,
                        posting.job_url,
                        description_hash,
                        now,
                        run_id,
                        json.dumps(posting.raw or {}, ensure_ascii=False),
                        company_key,
                        signature,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO jobs(
                        company_key, signature, source_job_id, title, location, job_url,
                        description_hash, first_seen_at, last_seen_at, first_run_id,
                        last_run_id, baseline, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        company_key,
                        signature,
                        posting.source_job_id,
                        posting.title,
                        posting.location,
                        posting.job_url,
                        description_hash,
                        now,
                        now,
                        run_id,
                        run_id,
                        1 if baseline else 0,
                        json.dumps(asdict(posting), ensure_ascii=False, default=str),
                    ),
                )
                if not baseline:
                    self.connection.execute(
                        """
                        INSERT INTO job_processing(
                            company_key, signature, status, created_at, updated_at
                        ) VALUES (?, ?, 'discovered', ?, ?)
                        """,
                        (company_key, signature, now, now),
                    )
                    new_postings.append(posting)
        self.connection.execute(
            "UPDATE companies SET initialized_at=COALESCE(initialized_at, ?), last_success_at=? WHERE company_key=?",
            (now, now, company_key),
        )
        self.connection.commit()
        return new_postings

    def get_processing_state(self, company_key: str, signature: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT * FROM job_processing
            WHERE company_key = ? AND signature = ?
            """,
            (company_key, signature),
        ).fetchone()
        return dict(row) if row else None

    def get_job(self, company_key: str, source_job_id: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT * FROM jobs
            WHERE company_key = ? AND source_job_id = ?
            """,
            (company_key, source_job_id),
        ).fetchone()
        return dict(row) if row else None

    def retryable_signatures(self, company_key: str) -> set[str]:
        return set(self.retryable_processing_states(company_key))

    def retryable_processing_states(self, company_key: str) -> dict[str, dict]:
        placeholders = ",".join("?" for _ in RETRYABLE_PROCESSING_STATUSES)
        rows = self.connection.execute(
            f"""
            SELECT * FROM job_processing
            WHERE company_key = ? AND status IN ({placeholders})
            """,
            (company_key, *sorted(RETRYABLE_PROCESSING_STATUSES)),
        ).fetchall()
        return {str(row["signature"]): dict(row) for row in rows}

    def llm_processing_records(self, company_key: str) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT
                p.*,
                j.source_job_id,
                j.title,
                j.location,
                j.job_url,
                j.first_seen_at,
                j.raw_json,
                c.display_name AS company
            FROM job_processing AS p
            JOIN jobs AS j
              ON j.company_key = p.company_key AND j.signature = p.signature
            JOIN companies AS c
              ON c.company_key = p.company_key
            WHERE p.company_key = ? AND p.status IN ('llm_pending', 'llm_failed')
            ORDER BY j.first_seen_at, j.source_job_id
            """,
            (company_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def review_queue_records(self, company_key: str | None = None) -> list[dict]:
        where = "WHERE p.status = 'review_queue'"
        parameters: tuple[str, ...] = ()
        if company_key is not None:
            where += " AND p.company_key = ?"
            parameters = (company_key,)
        rows = self.connection.execute(
            f"""
            SELECT
                p.*,
                j.source_job_id,
                j.title,
                j.location,
                j.job_url,
                j.first_seen_at,
                j.raw_json,
                c.display_name AS company
            FROM job_processing AS p
            JOIN jobs AS j
              ON j.company_key = p.company_key AND j.signature = p.signature
            JOIN companies AS c
              ON c.company_key = p.company_key
            {where}
            ORDER BY j.first_seen_at, j.source_job_id
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def transition_processing(
        self,
        company_key: str,
        signature: str,
        status: str,
        *,
        filter_reason: str = "",
        description: str | None = None,
        model: str = "",
        prompt_version: str = "",
        evaluation: dict | None = None,
        error: str = "",
    ) -> None:
        if status not in PROCESSING_STATUSES:
            raise ValueError(f"Unknown processing status: {status}")
        current = self.get_processing_state(company_key, signature)
        if current is None:
            raise KeyError(f"No processing row for {company_key}:{signature}")
        current_status = str(current["status"])
        if (
            status != current_status
            and status not in ALLOWED_PROCESSING_TRANSITIONS[current_status]
        ):
            raise ValueError(
                f"Invalid processing transition: {current_status} -> {status}"
            )

        now = utc_now()
        next_description = (
            str(current["description"]) if description is None else description
        )
        next_description_hash = (
            str(current["description_hash"])
            if description is None
            else hashlib.sha256(description.encode("utf-8")).hexdigest()
        )
        next_evaluation = (
            str(current["evaluation_json"])
            if evaluation is None
            else json.dumps(evaluation, ensure_ascii=False, sort_keys=True)
        )
        detail_attempts = int(current["detail_attempts"])
        llm_attempts = int(current["llm_attempts"])
        last_attempt_at = current["last_attempt_at"]
        if status == "detail_pending":
            detail_attempts += 1
            last_attempt_at = now
        elif status == "llm_pending" and current_status in {"llm_pending", "llm_failed"}:
            llm_attempts += 1
            last_attempt_at = now

        self.connection.execute(
            """
            UPDATE job_processing SET
                status=?, filter_reason=?, description=?, description_hash=?,
                detail_attempts=?, llm_attempts=?, last_attempt_at=?, model=?,
                prompt_version=?, evaluation_json=?, last_error=?, updated_at=?,
                completed_at=?
            WHERE company_key=? AND signature=?
            """,
            (
                status,
                filter_reason or str(current["filter_reason"]),
                next_description,
                next_description_hash,
                detail_attempts,
                llm_attempts,
                last_attempt_at,
                model or str(current["model"]),
                prompt_version or str(current["prompt_version"]),
                next_evaluation,
                error,
                now,
                now if status in TERMINAL_PROCESSING_STATUSES else None,
                company_key,
                signature,
            ),
        )
        self.connection.commit()

    def record_company_run(
        self,
        run_id: int,
        company_key: str,
        status: str,
        jobs_found: int = 0,
        new_jobs: int = 0,
        title_candidates: int = 0,
        prefilter_rejected: int = 0,
        detail_succeeded: int = 0,
        detail_failed: int = 0,
        llm_succeeded: int = 0,
        llm_failed: int = 0,
        review_queue_added: int = 0,
        error: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO run_companies(
                run_id, company_key, status, jobs_found, new_jobs, title_candidates,
                prefilter_rejected, detail_succeeded, detail_failed, llm_succeeded,
                llm_failed, review_queue_added, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                company_key,
                status,
                jobs_found,
                new_jobs,
                title_candidates,
                prefilter_rejected,
                detail_succeeded,
                detail_failed,
                llm_succeeded,
                llm_failed,
                review_queue_added,
                error,
            ),
        )
        self.connection.commit()
