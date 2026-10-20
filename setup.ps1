$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3 is required and was not found on PATH."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js and npm are required and were not found on PATH."
}

if (-not (Test-Path $venvPython)) {
    python -m venv (Join-Path $root ".venv")
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt")
Push-Location $root
try {
    npm install --no-audit --no-fund
} finally {
    Pop-Location
}

$profile = Join-Path $root "config\candidate_profile.json"
$policy = Join-Path $root "config\search_policy.json"
if (-not (Test-Path $profile)) {
    Copy-Item (Join-Path $root "config\candidate_profile.example.json") $profile
}
if (-not (Test-Path $policy)) {
    Copy-Item (Join-Path $root "config\search_policy.example.json") $policy
}

& $venvPython (Join-Path $root "tools\create_workbook.py")

Write-Host ""
Write-Host "Setup complete."
Write-Host "1. Edit config\candidate_profile.json."
Write-Host "2. Add companies and career URLs to outputs\job_finder.xlsx."
Write-Host "3. Set OPENAI_API_KEY, then run: .\run.ps1 initialize"
