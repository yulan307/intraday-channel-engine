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
    $configFile = "live_config.yaml"
    $remainingArgs = $args
    if ($args.Count -gt 0 -and -not $args[0].StartsWith("-")) {
        $configFile = $args[0]
        $remainingArgs = @($args | Select-Object -Skip 1)
    }
    if ([IO.Path]::GetFileName($configFile) -ne $configFile) {
        throw "Config input must be a YAML filename under configs: $configFile"
    }
    # The optional first argument is a configs/ filename; other CLI overrides pass through unchanged.
    & $python -m single_day_test.application.live_cli --config (Join-Path "configs" $configFile) @remainingArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
