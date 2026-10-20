# Daily Job Pipeline Design

Status: Approved for implementation
Date: 2026-10-14

## 1. Objective

Each manual daily run will reuse the same company connectors as initialization, compare the latest scrape with the local SQLite baseline, and evaluate only newly discovered job IDs.

The daily run will:

1. Scrape every active company in the workbook.
2. Save newly discovered source job IDs in SQLite.
3. Apply deterministic title, location, work-arrangement, and employment-type filters.
4. Fetch the full job description only for jobs that pass those filters.
5. Use one LLM to apply the experience and sponsorship rules and assign a relevance score.
6. During calibration, write every successfully evaluated job to a `Review Queue` sheet.
7. Record counts and failures in `Run Summary`.

SQLite remains the authoritative processing state. Excel remains the human-facing company configuration, review queue, and run report.

## 2. Scope

### Included

- The current workbook-driven company list and existing connectors.
- Delta detection using each company's source job ID.
- Only full-time, onsite or hybrid roles in California, New York State, or the Boston metro area.
- Target families such as data engineering, AI/ML engineering, data-focused software engineering, and data/technical product management.
- Maximum acceptable stated minimum experience of 3 years.
- Rejection when the job explicitly says sponsorship is unavailable; silence about sponsorship is treated as eligible.
- A configurable OpenAI model and a versioned evaluation prompt.
- Retryable description and LLM failures.

### Not included yet

- Automatic scheduling, notifications, or applications.
- Automatic support for an arbitrary new custom career portal.
- A production accept/reject threshold. That will be selected from the labeled Review Queue.
- A second LLM in the normal pipeline.

## 3. Processing flow

```text
Workbook companies
      |
      v
Existing connector fetches all current IDs
      |
      v
SQLite identifies new signatures only
      |
      v
Deterministic filters: title + location + work arrangement + employment type
      |
      +-----------------------> rejected with reason; no API call
      |
      v
Fetch and clean full job description
      |
      +-----------------------> failed; keep retryable
      |
      v
One structured LLM evaluation
      |
      +-----------------------> failed; keep retryable
      |
      v
Persist evaluation -> Review Queue -> Run Summary
```

The connector fetch used by initialization and the daily pipeline must be the same implementation. Initialization establishes the baseline; a daily run updates `last_seen` for existing jobs and creates processing records only for new signatures.

## 4. Persistence and retry safety

The existing `jobs` table continues to record discovery history. The implemented `job_processing` table keeps downstream state separate from discovery, so finding an ID does not incorrectly mean that its description and LLM evaluation succeeded. Existing baseline rows are not backfilled into this table; only future non-baseline discoveries are queued automatically.

Recommended evaluation fields:

- `company_key` and `signature`
- `status`
- `attempt_count` and `last_attempt_at`
- deterministic filter result and rejection reason
- cleaned description and `description_hash`
- `model` and `prompt_version`
- structured evaluation JSON
- score, recommendation, and evidence fields used by Excel
- last error and completion timestamp

Status transitions:

```text
discovered
  -> prefilter_rejected
  -> detail_pending -> detail_failed -> detail_pending
                    -> llm_pending -> llm_failed -> llm_pending
                                   -> review_queue
```

Rules:

- `detail_failed` and `llm_failed` remain eligible for retry on later daily runs even though the job ID is already in `jobs`.
- A successfully evaluated job is not sent to the API again when its description hash, prompt version, and model are unchanged.
- If the workbook export fails after the database commit, the export can be rebuilt from SQLite without another API call.
- A failed company scrape must not change that company's successful baseline state.
- A workbook row is unique by job signature plus prompt version, making reruns idempotent.

## 5. Deterministic prefilter

This stage makes no API call.

### Title

Use token and phrase families rather than exact title matches. The existing title policy is the starting point. In particular:

- Data engineer, AI engineer, ML engineer, relevant technical product roles, and similar variants can pass.
- A generic software engineer title must also contain a data/ML/AI/platform signal.
- Internships, co-ops, sales, support, staff, principal, research scientist, and engineering-manager roles are rejected.

Every decision stores its matched signals and rejection reason so false negatives can be diagnosed.

### Location and arrangement

- Accept California anywhere in the state.
- Accept New York anywhere in the state, including abbreviations such as NY and NYC.
- Accept Boston and configured Boston-metro aliases.
- Accept onsite and hybrid.
- Reject remote-only roles.

### Employment type

- Reject explicit internship, co-op, contract, temporary, and part-time roles.
- Accept explicit full-time roles.
- If employment type is absent from listing metadata, continue to description retrieval; do not reject only because the field is missing.

## 6. Job-description retrieval

Each connector will expose the same detail contract:

```text
fetch_job_detail(connector, connector_key, posting)
    -> description, employment_type, location_type, raw_detail
```

It is called only for newly discovered jobs that pass the deterministic prefilter. HTML and repeated boilerplate are removed before the LLM request.

Current implementation assessment:

| Company | Current description path | Daily-pipeline work |
|---|---|---|
| Scale AI | Greenhouse listing response contains content | Implemented and live-verified |
| Tesla | Browser helper per-job detail request | Implemented and live-verified |
| Salesforce | Feed includes `Job_Description` | Implemented with a process-local cache; live-verified |
| LinkedIn | SmartRecruiters per-posting detail endpoint | Implemented and live-verified |
| Apple | Rendered job-detail page | Implemented with the shared browser helper; live-verified |
| Microsoft | `JobPosting` structured data on the detail page | Implemented and live-verified |
| Meta | Rendered job-detail page | Implemented with the shared browser helper; live-verified |
| Adobe | Rendered job-detail page | Implemented with the shared browser helper; live-verified |

Each detail adapter must be verified against live examples before enabling LLM calls for that company.

## 7. LLM evaluation

Use one request per shortlisted job. The model receives:

- title, location, arrangement, and employment metadata;
- the cleaned job description;
- `config/candidate_profile.json`, not the original resume PDFs;
- the hard rules from `config/search_policy.json`;
- a strict output schema.

Required structured output:

```json
{
  "minimum_years_required": null,
  "experience_evidence": "",
  "experience_pass": true,
  "sponsorship_status": "not_mentioned",
  "sponsorship_evidence": null,
  "sponsorship_pass": true,
  "role_fit": "high",
  "relevance_score": 0,
  "recommended_accept": false,
  "decision_reason": "",
  "matching_evidence": [],
  "gaps": []
}
```

Allowed sponsorship states are `explicit_available`, `explicit_unavailable`, `not_mentioned`, and `unclear`. Allowed role-fit values are `high`, `medium`, and `low`. `relevance_score` is an integer from 0 to 100.

Hard interpretation rules:

- A stated minimum of 0–3 years passes.
- A range such as 3–5 years passes because its minimum is 3.
- An explicit minimum of 4 or more years fails.
- No stated experience requirement remains eligible.
- Explicitly unavailable sponsorship fails.
- No sponsorship statement remains eligible.
- The relevance score measures resume-to-role fit; it must not override a failed hard rule.

The API response will use Structured Outputs so required fields and enum values are enforced. The model name, reasoning setting, and prompt version remain configuration values rather than code constants.

## 8. Review Queue calibration

During calibration, every successfully evaluated job is written to `Review Queue`, including low-scoring and model-rejected examples. This avoids choosing a threshold before seeing actual errors.

Columns:

- date found, company, title, location, employment type, and URL
- source job ID/signature
- minimum experience and evidence
- sponsorship status and evidence
- relevance score and model recommendation
- decision reason, matching evidence, and gaps
- model and prompt version
- `User Verdict` (`Accept`, `Reject`, or `Unsure`) and `User Notes`

After a useful labeled sample is collected, compare the LLM recommendation and score with `User Verdict`. The production threshold and any prompt/rule adjustments will then be chosen from observed precision and recall. In the later production mode, only accepted roles need to appear in the main results sheet.

## 9. Model and API configuration

Initial configuration:

```text
OPENAI_API_KEY=<stored in the Windows environment, never in Excel or source control>
JOB_FINDER_MODEL=gpt-6-luna
JOB_FINDER_REASONING=medium
JOB_FINDER_PROMPT_VERSION=v1
```

`gpt-6-luna` is the initial model because this is a focused, structured triage task and the daily volume is modest but recurring. During calibration, a small set of borderline examples can also be evaluated with `gpt-6.1-sol`. Sol becomes the production choice only if it materially improves the user's labels; this comparison is an experiment, not a permanent two-model pipeline.

ChatGPT Plus usage is separate from API billing. The API account needs billing/credits and an API key, but a specific model is selected in the request rather than purchased separately.

Official references:

- [OpenAI model selection](https://developers.openai.com/api/docs/guides/model-selection)
- [OpenAI model catalog and pricing](https://developers.openai.com/api/docs/models)
- [OpenAI API quickstart](https://developers.openai.com/api/docs/quickstart)
- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

## 10. Run Summary

Add or retain per-company counters for:

- jobs found
- new IDs
- deterministic passes and rejections
- description successes and failures
- LLM successes and failures
- Review Queue rows added
- company-level error

The overall run can be `success`, `partial`, or `failed`. One company's failure must not prevent other companies from completing.

## 11. Verification and implementation order

Implement incrementally:

1. Add the common detail-fetch contract and verify one live job description per company.
2. Add evaluation state and retry-safe database transitions.
3. Add the Review Queue sheet/export and extended run counters.
4. Add the OpenAI client with mocked tests and strict structured output. **Implemented.**
5. Add a calibration command that can evaluate deliberately selected existing jobs without pretending they are new daily deltas. **Implemented.**
6. Run a small calibration set, label the rows, and compare Luna with Sol only on disputed or borderline cases.
7. Set the production threshold and switch the main output to accepted roles only.

Completion criteria for the first daily-pipeline version:

- New IDs are detected using the initialized baseline.
- All deterministic rejections include a reason and incur no API cost.
- Every shortlisted job either reaches Review Queue or remains visibly retryable.
- Rerunning does not duplicate workbook rows or repeat completed API calls.
- No API key is stored in the repository, database, logs, or workbook.
