# run-provisioner.ps1 — Wrapper for Windows Task Scheduler.
#
# Loads env vars from .env.provisioner (one-time dump from Azure),
# then runs provisioner.py against whatever Latitude jobs are
# currently unprovisioned.
#
# First-time setup — generate .env.provisioner from Azure:
#   az containerapp show --name vg-setup --resource-group VGA_group `
#     --query "properties.template.containers[0].env[].[name,value]" -o tsv |
#     ForEach-Object { $_ -replace "`t","=" } |
#     Out-File -FilePath .env.provisioner -Encoding utf8
#
# Usage (manual test):
#   pwsh -ExecutionPolicy Bypass -File .\run-provisioner.ps1
#
# Scheduled task registration: see the comment block at the bottom.

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

# --- Load env vars from .env.provisioner file --------------------------
$envFile = Join-Path $repo ".env.provisioner"
if (-not (Test-Path $envFile)) {
    Write-Log "FATAL  .env.provisioner not found. Generate it with:"
    Write-Log "  az containerapp show --name vg-setup --resource-group VGA_group ``"
    Write-Log "    --query `"properties.template.containers[0].env[].[name,value]`" -o tsv |"
    Write-Log "    ForEach-Object { `$_ -replace '``t','=' } |"
    Write-Log "    Out-File -FilePath .env.provisioner -Encoding utf8"
    exit 2
}

Write-Log "loading env vars from $envFile"
Get-Content $envFile -Encoding utf8 | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $idx   = $line.IndexOf("=")
        $name  = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        if ($name -and $value) {
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

# --- Email notification (opt-in) ---------------------------------------
# Set these to get an email summary whenever jobs get auto-provisioned.
# Graph credentials (GRAPH_*) come from the Container App env vars above.
# The sender mailbox must exist in the Azure AD tenant.
# Requires Mail.Send application permission on the app registration.
if (-not $env:NOTIFY_EMAIL_FROM) { $env:NOTIFY_EMAIL_FROM = "alerts@velocitygeomatics.ca" }
if (-not $env:NOTIFY_EMAIL_TO)   { $env:NOTIFY_EMAIL_TO   = "norm.dickson@magnussolutions.ca" }

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
