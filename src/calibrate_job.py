from __future__ import annotations

import argparse
from pathlib import Path

from connectors import ConnectorError, fetch_job_detail, fetch_jobs
from job_finder import (
    DEFAULT_DATABASE,
    DEFAULT_POLICY,
    DEFAULT_PROFILE,
    DEFAULT_PROMPT_DIRECTORY,
    DEFAULT_WORKBOOK,
    _deterministic_rejection_reason,
)
from llm_evaluator import EvaluationConfig, EvaluationError, evaluate_job, load_json
from policy import (
    clean_job_description,
    employment_type_prefilter,
    load_policy,
    location_is_allowed,
)
from storage import Store, make_signature
from workbook_io import (
    append_review_queue_jobs,
    read_companies,
    review_queue_contains,
)


def _select_company(companies: list[dict], requested: str) -> dict:
    matches = [
        company
        for company in companies
        if requested.casefold()
        in {company["company"].casefold(), company["company_key"].casefold()}
    ]
    if len(matches) != 1:
        raise ValueError(f"Could not uniquely identify company: {requested}")
    return matches[0]


def run_calibration(
    *,
    company_name: str,
    source_job_id: str,
    workbook_path: Path,
    database_path: Path,
    policy_path: Path,
    profile_path: Path,
    model: str = "",
    reasoning: str = "",
    prompt_version: str = "",
    dry_run: bool = False,
) -> int:
    companies = read_companies(workbook_path)
    company = _select_company(companies, company_name)
    policy = load_policy(policy_path)
    candidate_profile = load_json(profile_path)
    config = EvaluationConfig.from_environment().with_overrides(
        model=model,
        reasoning_effort=reasoning,
        prompt_version=prompt_version,
    )
    signature = make_signature(company["company_key"], source_job_id)
    if review_queue_contains(workbook_path, signature, config.prompt_version):
        print(
            f"SKIP {company['company']} {source_job_id}: already present in Review Queue "
            f"for prompt {config.prompt_version}"
        )
        return 0

    store = Store(database_path)
    try:
        stored_job = store.get_job(company["company_key"], source_job_id)
        if stored_job is None:
            raise ValueError(
                f"Job {source_job_id} is not in the initialized baseline/database"
            )
        if not bool(stored_job["baseline"]):
            raise ValueError(
                f"Job {source_job_id} is a daily delta, not a baseline calibration job"
            )
    finally:
        store.close()

    postings = fetch_jobs(company["connector"], company["connector_key"])
    posting = next(
        (item for item in postings if item.source_job_id == source_job_id), None
    )
    if posting is None:
        raise ConnectorError(
            f"Job {source_job_id} is no longer present on {company['company']}'s board"
        )
    rejection_reason = _deterministic_rejection_reason(posting, policy)
    if rejection_reason:
        raise ValueError(
            f"Job {source_job_id} does not pass the deterministic filter: "
            f"{rejection_reason}"
        )

    detail = fetch_job_detail(
        company["connector"], company["connector_key"], posting
    )
    employment_pass, employment_reason = employment_type_prefilter(
        detail.employment_type, policy
    )
    if not employment_pass:
        raise ValueError(f"Job detail was rejected: {employment_reason}")
    work_arrangement = detail.location_type or posting.location_type
    if not location_is_allowed(posting.location, work_arrangement, policy):
        raise ValueError("Job detail identifies a disallowed work arrangement")
    description = clean_job_description(detail.description)
    if not description:
        raise ConnectorError(f"Job {source_job_id} has an empty description")

    print(
        f"SELECTED {company['company']} | {posting.title} | {posting.location} | "
        f"description_chars={len(description)}"
    )
    if dry_run:
        print("DRY RUN: no OpenAI request was made and the workbook was not changed")
        return 0

    evaluation = evaluate_job(
        job={
            "company": company["company"],
            "title": posting.title,
            "location": posting.location,
            "work_arrangement": work_arrangement,
            "employment_type": detail.employment_type or posting.employment_type,
        },
        description=description,
        candidate_profile=candidate_profile,
        search_policy=policy,
        prompt_directory=DEFAULT_PROMPT_DIRECTORY,
        config=config,
    )
    years = evaluation["minimum_years_required"]
    review_row = {
        "date_found": stored_job["first_seen_at"],
        "company": company["company"],
        "title": posting.title,
        "location": posting.location,
        "work_arrangement": work_arrangement,
        "employment_type": detail.employment_type or posting.employment_type,
        "experience_required": (
            "Not stated" if years is None else f"{years:g}+ years minimum"
        ),
        "experience_evidence": evaluation["experience_evidence"],
        "sponsorship_status": evaluation["sponsorship_status"],
        "sponsorship_evidence": evaluation["sponsorship_evidence"],
        "relevance_score": evaluation["relevance_score"],
        "model_recommendation": (
            "Accept" if evaluation["recommended_accept"] else "Reject"
        ),
        "decision_reason": evaluation["decision_reason"],
        "matching_evidence": evaluation["matching_evidence"],
        "gaps": evaluation["gaps"],
        "job_url": posting.job_url,
        "source_job_id": posting.source_job_id,
        "signature": signature,
        "model": config.model,
        "prompt_version": config.prompt_version,
        "user_notes": "Calibration test on an initialized baseline role; not a new daily delta.",
    }
    added = append_review_queue_jobs(workbook_path, [review_row])
    usage = evaluation.get("api_usage", {})
    print(
        f"ADDED {added} Review Queue row | score={evaluation['relevance_score']} | "
        f"recommendation={review_row['model_recommendation']} | "
        f"tokens={usage.get('total_tokens', 'unavailable')}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate one initialized baseline job for calibration"
    )
    parser.add_argument("--company", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--model", default="")
    parser.add_argument("--reasoning", default="")
    parser.add_argument("--prompt-version", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    try:
        raise SystemExit(
            run_calibration(
                company_name=arguments.company,
                source_job_id=arguments.job_id,
                workbook_path=arguments.workbook,
                database_path=arguments.database,
                policy_path=arguments.policy,
                profile_path=arguments.profile,
                model=arguments.model,
                reasoning=arguments.reasoning,
                prompt_version=arguments.prompt_version,
                dry_run=arguments.dry_run,
            )
        )
    except (ConnectorError, EvaluationError, ValueError) as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(2)
