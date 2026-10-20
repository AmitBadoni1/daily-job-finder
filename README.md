# Daily Job Finder

A local, Excel-based job tracker that remembers current openings, finds only the daily delta, and uses AI to review promising new roles against your profile.

## How to use it

Requirements: Windows, Python 3, Node.js, Chrome or Edge, and an OpenAI API key.

```powershell
git clone https://github.com/AmitBadoni1/daily-job-finder.git
cd daily-job-finder
powershell -ExecutionPolicy Bypass -File .\setup.ps1
setx OPENAI_API_KEY "your-key"
```

Open a new terminal, replace the example content in `config/candidate_profile.json`, then open `outputs/job_finder.xlsx`. Enable the included companies you want or add a company and its careers URL.

```powershell
.\run.ps1 initialize   # run once to establish the baseline
.\run.ps1              # run each day
```

The API key is the only secret. Search rules live in `config/search_policy.json`; optional model overrides are `JOB_FINDER_MODEL`, `JOB_FINDER_REASONING`, and `JOB_FINDER_PROMPT_VERSION`.

## What it does under the hood

Initialization discovers or verifies a company's connector and stores current job IDs in local SQLite. Each daily run fetches the same boards, calculates the unseen IDs, rejects obvious title/location/employment mismatches locally, retrieves descriptions only for survivors, and sends only those new jobs to the model. Results and run health are appended to the Excel workbook.

Greenhouse, Ashby, and Lever are discovered automatically. The repository also includes connectors for Tesla, Apple, Microsoft, Meta, Adobe, Salesforce, SmartRecruiters, and LinkedIn's SmartRecruiters board. Other custom career sites need a connector implementation.

Licensed under the MIT License.
