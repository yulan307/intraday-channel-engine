$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Project Python was not found: $python"
    exit 1
}

Push-Location $projectRoot
$exitCode = 1
try {
    # All supplied arguments, including --help, are forwarded unchanged.
    & $python -m single_day_test.application.backtest_cli @args
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
