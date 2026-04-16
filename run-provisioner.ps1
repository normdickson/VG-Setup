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

$ErrorActionPreference = "Stop"

# Python's logging module writes to stderr. Under PS 7 with ErrorActionPreference=Stop,
# that would be treated as a terminating error on the first log line. Opt out so only
# a non-zero exit code from python is treated as failure.
$PSNativeCommandUseErrorActionPreference = $false

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

# --- Logging ------------------------------------------------------------
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log    = Join-Path $logDir ("provisioner-{0}.log" -f (Get-Date -Format yyyyMMdd))

function Write-Log {
    param([string]$Message)
    "$(Get-Date -Format o)  $Message" | Tee-Object -FilePath $log -Append
}

Write-Log "=== provisioner run starting ==="

try {
    # --- Pull env vars + secrets from the Container App ----------------
    Write-Log "fetching env vars from Container App vg-setup/VGA_group"
    $envs = az containerapp show --name vg-setup --resource-group VGA_group `
              --query "properties.template.containers[0].env" -o json | ConvertFrom-Json

    foreach ($e in $envs) {
        if ($e.value) {
            Set-Item -Path "Env:$($e.name)" -Value $e.value
        } elseif ($e.secretRef) {
            $secret = az containerapp secret show --name vg-setup --resource-group VGA_group `
                        --secret-name $e.secretRef --query value -o tsv
            Set-Item -Path "Env:$($e.name)" -Value $secret
        }
    }

    # --- Run the provisioner -------------------------------------------
    # Safety defaults: 7-day lookback, cap at 5 jobs per run.
    # Remove --max / shorten --lookback once you're comfortable.
    Write-Log "running: python provisioner.py --lookback 7 --max 5 -v"
    # Merge streams (stdout + stderr) so Python log lines end up in the file.
    & python provisioner.py --lookback 7 --max 5 -v 2>&1 | Tee-Object -FilePath $log -Append
    $code = $LASTEXITCODE
    Write-Log "=== provisioner run complete (exit=$code) ==="
    exit $code
}
catch {
    Write-Log "FATAL  $($_.Exception.Message)"
    if ($_.ScriptStackTrace) { Write-Log $_.ScriptStackTrace }
    exit 2
}

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
