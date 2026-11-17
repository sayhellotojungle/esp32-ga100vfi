$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'run_tests.py') @args
exit $LASTEXITCODE
