$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot '..\tools\sync_arduino.py') @args
exit $LASTEXITCODE
