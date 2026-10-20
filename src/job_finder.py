from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from connectors import (
    ConnectorError,
    JobPosting,
    UnsupportedConnectorError,
    fetch_job_detail,
    fetch_jobs,
)
from policy import (
    clean_job_description,
    employment_type_prefilter,
    load_policy,
    location_is_allowed,
    title_prefilter,
)
from llm_evaluator import (
    EvaluationConfig,
    EvaluationError,
    evaluate_job,
    load_json,
)
from storage import Store, make_signature
from workbook_io import append_review_queue_jobs, append_run_rows, read_companies


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKBOOK = ROOT / "outputs" / "job_finder.xlsx"
DEFAULT_DATABASE = ROOT / "data" / "job_finder.db"
DEFAULT_POLICY = ROOT / "config" / "search_policy.json"
DEFAULT_PROFILE = ROOT / "config" / "candidate_profile.json"
DEFAULT_PROMPT_DIRECTORY = ROOT / "config"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _deterministic_rejection_reason(posting: JobPosting, policy: dict) -> str:
    title_pass, title_reason = title_prefilter(posting.title, policy)
    if not title_pass:
        return f"title: {title_reason}"
    arrangement_hints = " ".join(
        item for item in (posting.location_type, posting.title) if item
    )
    if not location_is_allowed(posting.location, arrangement_hints, policy):
        return "location or work arrangement is outside the search policy"
    employment_pass, employment_reason = employment_type_prefilter(
        posting.employment_type, policy
    )
    if not employment_pass:
        return f"employment: {employment_reason}"
    return ""


def process_retryable_postings(
    store: Store,
    company: dict,
    postings: list[JobPosting],
    policy: dict,
) -> dict[str, int]:
    company_key = company["company_key"]
    states = store.retryable_processing_states(company_key)
    postings_by_signature = {
        make_signature(company_key, posting.source_job_id): posting for posting in postings
    }
    counts = {
        "prefilter_passed": 0,
        "prefilter_rejected": 0,
        "detail_succeeded": 0,
        "detail_failed": 0,
    }

    for signature, state in states.items():
        posting = postings_by_signature.get(signature)
        if posting is None:
            if state["status"] in {"discovered", "detail_pending", "detail_failed"}:
                store.transition_processing(
                    company_key,
                    signature,
                    "prefilter_rejected",
                    filter_reason=(
                        "job is no longer present in the company's current listings"
                    ),
                )
                counts["prefilter_rejected"] += 1
            continue
        status = state["status"]
        if status in {"llm_pending", "llm_failed"}:
            continue
        if status in {"discovered", "detail_failed"}:
            rejection_reason = _deterministic_rejection_reason(posting, policy)
            if rejection_reason:
                store.transition_processing(
                    company_key,
                    signature,
                    "prefilter_rejected",
                    filter_reason=rejection_reason,
                )
                counts["prefilter_rejected"] += 1
                continue
            counts["prefilter_passed"] += 1

        store.transition_processing(company_key, signature, "detail_pending")
        try:
            detail = fetch_job_detail(
                company["connector"], company["connector_key"], posting
            )
            employment_pass, employment_reason = employment_type_prefilter(
                detail.employment_type, policy
            )
            if not employment_pass:
                store.transition_processing(
                    company_key,
                    signature,
                    "prefilter_rejected",
                    filter_reason=f"employment: {employment_reason}",
                )
                counts["prefilter_rejected"] += 1
                continue
            arrangement_hints = " ".join(
                item
                for item in (
                    detail.location_type,
                    posting.location_type,
                    posting.title,
                )
                if item
            )
            if not location_is_allowed(posting.location, arrangement_hints, policy):
                store.transition_processing(
                    company_key,
                    signature,
                    "prefilter_rejected",
                    filter_reason="detail identifies a disallowed work arrangement",
                )
                counts["prefilter_rejected"] += 1
                continue
            description = clean_job_description(detail.description)
            if not description:
                raise ConnectorError(
                    f"Job {posting.source_job_id} has an empty cleaned description"
                )
            store.transition_processing(
                company_key,
                signature,
                "llm_pending",
                description=description,
            )
            counts["detail_succeeded"] += 1
        except ConnectorError as exc:
            store.transition_processing(
                company_key,
                signature,
                "detail_failed",
                error=str(exc),
            )
            counts["detail_failed"] += 1
    return counts


def _record_metadata(record: dict) -> dict:
    try:
        raw = json.loads(record.get("raw_json") or "{}")
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "company": record.get("company", ""),
        "title": record.get("title", ""),
        "location": record.get("location", ""),
        "work_arrangement": raw.get("location_type", ""),
        "employment_type": raw.get("employment_type", ""),
    }


def _review_queue_row(record: dict) -> dict:
    evaluation = json.loads(record["evaluation_json"])
    metadata = _record_metadata(record)
    years = evaluation.get("minimum_years_required")
    return {
        "date_found": record["first_seen_at"],
        "company": record["company"],
        "title": record["title"],
        "location": record["location"],
        "work_arrangement": metadata["work_arrangement"],
        "employment_type": metadata["employment_type"],
        "experience_required": "Not stated" if years is None else f"{years:g}+ years minimum",
        "experience_evidence": evaluation.get("experience_evidence", ""),
        "sponsorship_status": evaluation.get("sponsorship_status", ""),
        "sponsorship_evidence": evaluation.get("sponsorship_evidence", ""),
        "relevance_score": evaluation.get("relevance_score", ""),
        "model_recommendation": (
            "Accept" if evaluation.get("recommended_accept") else "Reject"
        ),
        "decision_reason": evaluation.get("decision_reason", ""),
        "matching_evidence": evaluation.get("matching_evidence", []),
        "gaps": evaluation.get("gaps", []),
        "job_url": record["job_url"],
        "source_job_id": record["source_job_id"],
        "signature": record["signature"],
        "model": record["model"],
        "prompt_version": record["prompt_version"],
    }


def evaluate_retryable_postings(
    store: Store,
    company: dict,
    postings: list[JobPosting],
    policy: dict,
    candidate_profile: dict,
    *,
    prompt_directory: Path = DEFAULT_PROMPT_DIRECTORY,
    config: EvaluationConfig | None = None,
) -> dict[str, int]:
    records = store.llm_processing_records(company["company_key"])
    counts = {"llm_succeeded": 0, "llm_failed": 0}
    if not records:
        return counts

    try:
        resolved_config = config or EvaluationConfig.from_environment()
    except EvaluationError as exc:
        for record in records:
            store.transition_processing(
                company["company_key"], record["signature"], "llm_pending"
            )
            store.transition_processing(
                company["company_key"],
                record["signature"],
                "llm_failed",
                error=str(exc),
            )
            counts["llm_failed"] += 1
        return counts

    postings_by_signature = {
        make_signature(company["company_key"], posting.source_job_id): posting
        for posting in postings
    }
    for record in records:
        signature = record["signature"]
        metadata = _record_metadata(record)
        posting = postings_by_signature.get(signature)
        if posting is not None:
            metadata.update(
                {
                    "title": posting.title,
                    "location": posting.location,
                    "work_arrangement": posting.location_type,
                    "employment_type": posting.employment_type,
                }
            )
        store.transition_processing(
            company["company_key"],
            signature,
            "llm_pending",
            model=resolved_config.model,
            prompt_version=resolved_config.prompt_version,
        )
        try:
            evaluation = evaluate_job(
                job=metadata,
                description=record["description"],
                candidate_profile=candidate_profile,
                search_policy=policy,
                prompt_directory=prompt_directory,
                config=resolved_config,
            )
            store.transition_processing(
                company["company_key"],
                signature,
                "review_queue",
                model=resolved_config.model,
                prompt_version=resolved_config.prompt_version,
                evaluation=evaluation,
            )
            counts["llm_succeeded"] += 1
        except EvaluationError as exc:
            store.transition_processing(
                company["company_key"],
                signature,
                "llm_failed",
                model=resolved_config.model,
                prompt_version=resolved_config.prompt_version,
                error=str(exc),
            )
            counts["llm_failed"] += 1
    return counts


def run_pipeline(
    workbook_path: Path,
    database_path: Path,
    policy_path: Path,
    mode: str,
    profile_path: Path = DEFAULT_PROFILE,
    company_name: str | None = None,
) -> int:
    policy = load_policy(policy_path)
    candidate_profile = load_json(profile_path)
    companies = read_companies(workbook_path)
    store = Store(database_path)
    store.sync_companies(companies)
    if company_name:
        requested = company_name.strip().casefold()
        companies = [
            company
            for company in companies
            if requested
            in {
                company["company"].strip().casefold(),
                company["company_key"].strip().casefold(),
            }
        ]
        if not companies:
            store.close()
            raise ValueError(f"No enabled initialized company matches {company_name!r}")
    run_id = store.start_run(mode)
    started_at = now()
    run_rows: list[dict] = []
    failures = 0
    unsupported = 0

    try:
        for company in companies:
            company_key = company["company_key"]
            company_name = company["company"]
            baseline_company = mode == "baseline" or not store.is_initialized(company_key)
            try:
                postings = fetch_jobs(company["connector"], company["connector_key"])
                new_postings = store.store_postings(
                    company_key,
                    postings,
                    run_id,
                    baseline=baseline_company,
                )
                processing = {
                    "prefilter_passed": 0,
                    "prefilter_rejected": 0,
                    "detail_succeeded": 0,
                    "detail_failed": 0,
                }
                evaluation = {"llm_succeeded": 0, "llm_failed": 0}
                review_queue_added = 0
                processing_errors: list[str] = []
                if not baseline_company:
                    processing = process_retryable_postings(
                        store, company, postings, policy
                    )
                    if processing["detail_failed"]:
                        processing_errors.append(
                            f"{processing['detail_failed']} job detail fetch(es) failed; "
                            "queued for retry"
                        )
                    evaluation = evaluate_retryable_postings(
                        store,
                        company,
                        postings,
                        policy,
                        candidate_profile,
                    )
                    if evaluation["llm_failed"]:
                        processing_errors.append(
                            f"{evaluation['llm_failed']} OpenAI evaluation(s) failed; "
                            "queued for retry"
                        )
                    try:
                        review_rows = [
                            _review_queue_row(record)
                            for record in store.review_queue_records(company_key)
                        ]
                        review_queue_added = append_review_queue_jobs(
                            workbook_path, review_rows
                        )
                    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                        processing_errors.append(
                            f"Review Queue export failed and will be retried: {exc}"
                        )
                processing_error = "; ".join(processing_errors)
                if processing_errors:
                    failures += 1
                status = (
                    "baseline"
                    if baseline_company
                    else "partial" if processing_error else "success"
                )
                store.record_company_run(
                    run_id,
                    company_key,
                    status,
                    jobs_found=len(postings),
                    new_jobs=0 if baseline_company else len(new_postings),
                    title_candidates=processing["prefilter_passed"],
                    prefilter_rejected=processing["prefilter_rejected"],
                    detail_succeeded=processing["detail_succeeded"],
                    detail_failed=processing["detail_failed"],
                    llm_succeeded=evaluation["llm_succeeded"],
                    llm_failed=evaluation["llm_failed"],
                    review_queue_added=review_queue_added,
                    error=processing_error,
                )
                run_rows.append(
                    {
                        "run_id": run_id,
                        "started_at": started_at,
                        "completed_at": now(),
                        "mode": "baseline" if baseline_company else mode,
                        "company": company_name,
                        "status": status,
                        "jobs_found": len(postings),
                        "new_jobs": 0 if baseline_company else len(new_postings),
                        "prefilter_passed": processing["prefilter_passed"],
                        "prefilter_rejected": processing["prefilter_rejected"],
                        "detail_succeeded": processing["detail_succeeded"],
                        "detail_failed": processing["detail_failed"],
                        "llm_succeeded": evaluation["llm_succeeded"],
                        "llm_failed": evaluation["llm_failed"],
                        "review_queue_added": review_queue_added,
                        "error": processing_error,
                    }
                )
            except UnsupportedConnectorError as exc:
                unsupported += 1
                store.record_company_run(run_id, company_key, "unsupported", error=str(exc))
                run_rows.append(
                    {
                        "run_id": run_id,
                        "started_at": started_at,
                        "completed_at": now(),
                        "mode": mode,
                        "company": company_name,
                        "status": "unsupported",
                        "jobs_found": 0,
                        "new_jobs": 0,
                        "prefilter_passed": 0,
                        "prefilter_rejected": 0,
                        "detail_succeeded": 0,
                        "detail_failed": 0,
                        "llm_succeeded": 0,
                        "llm_failed": 0,
                        "review_queue_added": 0,
                        "error": str(exc),
                    }
                )
            except ConnectorError as exc:
                failures += 1
                store.record_company_run(run_id, company_key, "failed", error=str(exc))
                run_rows.append(
                    {
                        "run_id": run_id,
                        "started_at": started_at,
                        "completed_at": now(),
                        "mode": mode,
                        "company": company_name,
                        "status": "failed",
                        "jobs_found": 0,
                        "new_jobs": 0,
                        "prefilter_passed": 0,
                        "prefilter_rejected": 0,
                        "detail_succeeded": 0,
                        "detail_failed": 0,
                        "llm_succeeded": 0,
                        "llm_failed": 0,
                        "review_queue_added": 0,
                        "error": str(exc),
                    }
                )
        store.finish_run(run_id, "partial" if failures or unsupported else "success")
    except Exception:
        store.finish_run(run_id, "failed")
        raise
    finally:
        store.close()

    append_run_rows(workbook_path, run_rows)
    return 0 if failures == 0 else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local daily job finder")
    parser.add_argument("mode", choices=["baseline", "run"])
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--company",
        help="Run only one enabled initialized company, by display name or company key",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    raise SystemExit(
        run_pipeline(
            arguments.workbook,
            arguments.database,
            arguments.policy,
            arguments.mode,
            arguments.profile,
            arguments.company,
        )
    )
