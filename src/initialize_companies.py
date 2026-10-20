from __future__ import annotations

import argparse
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from company_discovery import DiscoveryError, DiscoveryResult, discover_connector
from connectors import ConnectorError, SUPPORTED_CONNECTORS, fetch_jobs
from storage import Store
from workbook_io import (
    append_run_rows,
    read_company_rows,
    update_company_rows,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKBOOK = ROOT / "outputs" / "job_finder.xlsx"
DEFAULT_DATABASE = ROOT / "data" / "job_finder.db"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def company_key_from_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").lower()
    key = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")
    return key or "company"


def unique_company_key(name: str, used_keys: set[str]) -> str:
    base = company_key_from_name(name)
    candidate = base
    suffix = 2
    while candidate in used_keys:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_keys.add(candidate)
    return candidate


def _initializer_note(existing: str, message: str) -> str:
    base = re.sub(r"\s*Initializer:.*$", "", existing or "").strip()
    addition = f"Initializer: {message}"
    return f"{base} {addition}".strip()


def resolve_company(row: dict) -> DiscoveryResult:
    connector = row.get("connector", "").strip().lower()
    connector_key = row.get("connector_key", "").strip()
    careers_url = row.get("careers_url", "").strip()
    configured_error = ""

    if connector in SUPPORTED_CONNECTORS and connector_key:
        try:
            postings = fetch_jobs(connector, connector_key)
            return DiscoveryResult(
                connector=connector,
                connector_key=connector_key,
                careers_url=careers_url,
                status="verified",
                note=f"Verified configured {connector} feed with {len(postings)} current jobs.",
                postings=tuple(postings),
            )
        except ConnectorError as exc:
            configured_error = f"Configured feed failed: {exc}. "

    if not careers_url:
        return DiscoveryResult(
            connector=connector if connector != "pending" else "",
            connector_key=connector_key,
            careers_url="",
            status="needs_careers_url",
            note="Add the company's official careers URL, then run initialization again.",
        )

    result = discover_connector(careers_url)
    if configured_error:
        return DiscoveryResult(
            connector=result.connector,
            connector_key=result.connector_key,
            careers_url=result.careers_url,
            status=result.status,
            note=configured_error + result.note,
            postings=result.postings,
        )
    return result


def _matches_target(row: dict, target: str) -> bool:
    target_value = target.strip().casefold()
    return target_value in {
        row.get("company", "").strip().casefold(),
        row.get("company_key", "").strip().casefold(),
    }


def initialize_companies(
    workbook_path: Path,
    database_path: Path,
    *,
    target: str = "",
    retry_pending: bool = False,
    dry_run: bool = False,
) -> int:
    rows = read_company_rows(workbook_path)
    if target:
        rows = [row for row in rows if _matches_target(row, target)]
        if not rows:
            raise ValueError(f"No Companies row matches '{target}'")

    store = Store(database_path)
    used_keys = {
        row["company_key"]
        for row in read_company_rows(workbook_path)
        if row["company_key"]
    } | store.company_keys()
    repair_updates: list[dict] = []
    candidates: list[dict] = []

    try:
        for original in rows:
            row = dict(original)
            if not row["enabled"]:
                continue
            if not row["company_key"]:
                saved_match = store.find_company_by_display_name(row["company"])
                if saved_match:
                    row["company_key"] = saved_match["company_key"]
                else:
                    row["company_key"] = unique_company_key(row["company"], used_keys)

            if store.is_initialized(row["company_key"]):
                saved = store.get_company(row["company_key"]) or {}
                if (
                    row["status"].strip().lower() not in {"initialized", "ready"}
                    or not row["connector"]
                    or not row["connector_key"]
                ):
                    repair_updates.append(
                        {
                            "row_number": row["row_number"],
                            "company_key": row["company_key"],
                            "enabled": True,
                            "careers_url": saved.get("careers_url") or row["careers_url"],
                            "connector": saved.get("connector") or row["connector"],
                            "connector_key": saved.get("connector_key") or row["connector_key"],
                            "status": "Initialized",
                            "notes": _initializer_note(
                                row["notes"], "Workbook row repaired from the local baseline."
                            ),
                        }
                    )
                print(f"SKIP {row['company']}: baseline already exists")
                continue

            status = row["status"].strip().lower()
            is_new = status in {"", "new", "ready"}
            if not target and not retry_pending and not is_new:
                continue
            candidates.append(row)

        if dry_run:
            for row in candidates:
                result = resolve_company(row)
                print(
                    f"DRY RUN {row['company']}: {result.status}; "
                    f"connector={result.connector or '-'}; key={result.connector_key or '-'}; "
                    f"jobs={len(result.postings)}; {result.note}"
                )
            if repair_updates:
                print(f"DRY RUN: {len(repair_updates)} workbook row(s) would be repaired")
            if not candidates and not repair_updates:
                print("No companies need initialization")
            return 0

        if repair_updates:
            update_company_rows(workbook_path, repair_updates)

        if not candidates:
            if not repair_updates:
                print("No companies need initialization")
            return 0

        run_id = store.start_run("initialize")
        started_at = now()
        workbook_updates: list[dict] = []
        run_rows: list[dict] = []
        incomplete = 0

        for row in candidates:
            result_status = "failed"
            jobs_found = 0
            error = ""
            try:
                result = resolve_company(row)
                update = {
                    "row_number": row["row_number"],
                    "company_key": row["company_key"],
                    "enabled": True,
                    "careers_url": result.careers_url or row["careers_url"],
                    "connector": result.connector,
                    "connector_key": result.connector_key,
                    "notes": _initializer_note(row["notes"], result.note),
                }
                company = {
                    "company_key": row["company_key"],
                    "company": row["company"],
                    "careers_url": result.careers_url or row["careers_url"],
                    "connector": result.connector,
                    "connector_key": result.connector_key,
                }
                store.upsert_company(company)

                if result.status == "verified":
                    postings = list(result.postings)
                    store.store_postings(
                        row["company_key"], postings, run_id, baseline=True
                    )
                    jobs_found = len(postings)
                    result_status = "baseline"
                    update["status"] = "Initialized"
                    print(
                        f"INITIALIZED {row['company']}: {result.connector} "
                        f"({jobs_found} jobs baselined)"
                    )
                elif result.status == "needs_careers_url":
                    incomplete += 1
                    result_status = "needs_input"
                    update["status"] = "Needs careers URL"
                    error = result.note
                    print(f"NEEDS INPUT {row['company']}: {result.note}")
                elif result.status == "adapter_pending":
                    incomplete += 1
                    result_status = "unsupported"
                    update["status"] = "Adapter pending"
                    error = result.note
                    print(f"ADAPTER PENDING {row['company']}: {result.note}")
                else:
                    incomplete += 1
                    result_status = "needs_review"
                    update["status"] = "Needs review"
                    error = result.note
                    print(f"NEEDS REVIEW {row['company']}: {result.note}")
                workbook_updates.append(update)
            except (ConnectorError, DiscoveryError, ValueError) as exc:
                incomplete += 1
                error = str(exc)
                workbook_updates.append(
                    {
                        "row_number": row["row_number"],
                        "company_key": row["company_key"],
                        "enabled": True,
                        "status": "Initialization failed",
                        "notes": _initializer_note(row["notes"], error),
                    }
                )
                print(f"FAILED {row['company']}: {error}")

            store.record_company_run(
                run_id,
                row["company_key"],
                result_status,
                jobs_found=jobs_found,
                error=error,
            )
            run_rows.append(
                {
                    "run_id": run_id,
                    "started_at": started_at,
                    "completed_at": now(),
                    "mode": "initialize",
                    "company": row["company"],
                    "status": result_status,
                    "jobs_found": jobs_found,
                    "new_jobs": 0,
                    "prefilter_passed": 0,
                    "prefilter_rejected": 0,
                    "detail_succeeded": 0,
                    "detail_failed": 0,
                    "llm_succeeded": 0,
                    "llm_failed": 0,
                    "review_queue_added": 0,
                    "error": error,
                }
            )

        store.finish_run(run_id, "partial" if incomplete else "success")
        update_company_rows(workbook_path, workbook_updates)
        append_run_rows(workbook_path, run_rows)
        return 0 if incomplete == 0 else 2
    finally:
        store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover connectors and baseline newly added companies"
    )
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--company",
        default="",
        help="Initialize only this company name or company key",
    )
    parser.add_argument(
        "--retry-pending",
        action="store_true",
        help="Retry unresolved rows instead of processing only new rows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and verify without changing the workbook or job baseline",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    raise SystemExit(
        initialize_companies(
            arguments.workbook,
            arguments.database,
            target=arguments.company,
            retry_pending=arguments.retry_pending,
            dry_run=arguments.dry_run,
        )
    )
