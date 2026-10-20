param(
    [ValidateSet("run", "initialize", "baseline")]
    [string]$Command = "run",
    [string]$Company = ""
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

if ($Command -eq "initialize") {
    $arguments = @((Join-Path $root "src\initialize_companies.py"))
} else {
    $arguments = @((Join-Path $root "src\job_finder.py"), $Command)
}

if ($Company) {
    $arguments += @("--company", $Company)
}

& $python @arguments
exit $LASTEXITCODE
