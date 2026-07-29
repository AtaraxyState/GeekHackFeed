<#
Convenience wrapper for local Windows builds. The build itself lives in
build.py, which is what CI runs too -- one implementation, so a change cannot
work locally and break the release job.

    .\build.ps1
    .\build.ps1 -VersionName 1.2.0
    .\build.ps1 -Install
    .\build.ps1 -- --keystore release.jks --ks-pass env:KS_PASS
#>

param(
    [string]$VersionName = "",
    [switch]$Install,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# Deliberately not "Stop": javac writes ordinary notes to stderr, and under
# Stop PowerShell promotes those to terminating errors and kills the build.
$ErrorActionPreference = "Continue"

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "ERROR: python not found on PATH" -ForegroundColor Red
    exit 1
}

$argv = @((Join-Path $PSScriptRoot "build.py"))
if ($VersionName) { $argv += @("--version-name", $VersionName) }
if ($Install)     { $argv += "--install" }
if ($Rest)        { $argv += $Rest }

& $python @argv
exit $LASTEXITCODE
