# run-provisioner.ps1 — Wrapper for Windows Task Scheduler.
#
# Pulls env vars + secrets from the vg-setup Container App on every run
# (so we don't have to keep them in sync here), then runs provisioner.py
# against whatever Latitude jobs are currently unprovisioned.
#
# Usage (manual test):
#   powershell -ExecutionPolicy Bypass -File .\run-provisioner.ps1
#
# Scheduled task registration: see README (or the inline snippet below).

# Do NOT use $ErrorActionPreference = "Stop" here.
# Python's logging module writes INFO/DEBUG lines to stderr; under
# Windows PowerShell 5.1 (and PS 7 with PSNativeCommandUseErrorActionPreference)
# that would be treated as a terminating error the moment the provisioner
# starts. We rely on $LASTEXITCODE from python instead.
$ErrorActionPreference = "Continue"

# PS 7.3+ only — safely ignored on 5.1.
try { $PSNativeCommandUseErrorActionPreference = $false } catch {}

# Force UTF-8 end-to-end so the log file isn't garbled when running under
# Task Scheduler (no attached console → Python defaults to cp1252; PowerShell
# captures the bytes as UTF-16 or cp1252 → garbage). Setting PYTHONIOENCODING
# makes Python emit UTF-8; setting Console.OutputEncoding makes PowerShell
# read it as UTF-8; Out-File -Encoding utf8 writes it as UTF-8.
$env:PYTHONIOENCODING         = "utf-8"
$env:PYTHONUTF8               = "1"
[Console]::OutputEncoding     = [System.Text.Encoding]::UTF8
$OutputEncoding               = [System.Text.Encoding]::UTF8

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

# --- Logging ------------------------------------------------------------
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log    = Join-Path $logDir ("provisioner-{0}.log" -f (Get-Date -Format yyyyMMdd))

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format o)  $Message"
    Write-Host $line
    $line | Out-File -FilePath $log -Append -Encoding utf8
}

Write-Log "=== provisioner run starting ==="

# --- Pull env vars + secrets from the Container App --------------------
Write-Log "fetching env vars from Container App vg-setup/VGA_group"
$envsJson = az containerapp show --name vg-setup --resource-group VGA_group `
              --query "properties.template.containers[0].env" -o json
if ($LASTEXITCODE -ne 0 -or -not $envsJson) {
    Write-Log "FATAL  could not fetch env vars from Container App (az exit=$LASTEXITCODE)"
    exit 2
}

$envs = $envsJson | ConvertFrom-Json
foreach ($e in $envs) {
    if ($e.value) {
        Set-Item -Path "Env:$($e.name)" -Value $e.value
    } elseif ($e.secretRef) {
        $secret = az containerapp secret show --name vg-setup --resource-group VGA_group `
                    --secret-name $e.secretRef --query value -o tsv
        if ($LASTEXITCODE -eq 0 -and $secret) {
            Set-Item -Path "Env:$($e.name)" -Value $secret
        } else {
            Write-Log "WARN  could not read secret $($e.secretRef)"
        }
    }
}

# --- Run the provisioner -----------------------------------------------
# Safety defaults: 7-day lookback, cap at 5 jobs per run.
# Remove --max / shorten --lookback once you're comfortable.
Write-Log "running: python provisioner.py --lookback 7 --max 5 -v"

# Merge stderr into stdout and write to the log as UTF-8 explicitly.
& python provisioner.py --lookback 7 --max 5 -v 2>&1 |
    ForEach-Object { "$_" } |
    Tee-Object -FilePath $log -Append -Encoding utf8
$code = $LASTEXITCODE

Write-Log "=== provisioner run complete (exit=$code) ==="
exit $code

# -----------------------------------------------------------------------
# One-time scheduled-task registration (run once in an admin PowerShell):
# -----------------------------------------------------------------------
#   $action    = New-ScheduledTaskAction -Execute "powershell.exe" `
#                 -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\deploy\VG-Setup\run-provisioner.ps1" `
#                 -WorkingDirectory "C:\deploy\VG-Setup"
#
#   $trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
#                 -RepetitionInterval (New-TimeSpan -Minutes 15) `
#                 -RepetitionDuration ([TimeSpan]::MaxValue)
#
#   $settings  = New-ScheduledTaskSettingsSet `
#                 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
#                 -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
#
#   $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
#                 -LogonType S4U -RunLevel Limited
#
#   Register-ScheduledTask -TaskName "VG-Setup Provisioner" `
#                          -Action $action -Trigger $trigger `
#                          -Settings $settings -Principal $principal `
#                          -Description "Polls Latitude and provisions new jobs every 15 min"
