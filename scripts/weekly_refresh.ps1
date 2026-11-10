# Weekly refresh for Windows Task Scheduler.
#
# Registered to start daily, but it only works once a week: from each Saturday until a run
# succeeds. A laptop can't promise to stay awake for one fixed time (closing the lid killed
# two test runs), so a missed or interrupted run tries again the next day.
#
# Register once:
#   $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
#              -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PWD\scripts\weekly_refresh.ps1`""
#   $trigger = New-ScheduledTaskTrigger -Daily -At 7am
#   $options = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 4
#   Register-ScheduledTask -TaskName "value_screener weekly" -Action $action -Trigger $trigger -Settings $options
#
# It runs while you're logged on and the PC is awake, for 20 to 75 minutes. Delete
# data\logs\last_success.txt to force a run. Logs go to data\logs\refresh_YYYY-MM-DD.log.

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("refresh_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

$marker = Join-Path $logDir "last_success.txt"
$today = (Get-Date).Date
$lastSaturday = $today.AddDays(-(([int]$today.DayOfWeek + 1) % 7))
if ((Test-Path $marker) -and ([datetime](Get-Content $marker)) -ge $lastSaturday) { exit 0 }

$python = Join-Path $repo ".venv\Scripts\python.exe"

# Through cmd, not PowerShell's *>&1: PowerShell 5.1 turns a program's stderr into error
# records, so one Python warning would stop this script. -u keeps the log current if it dies.
function Step($command) {
    cmd /c "`"$python`" -u -m value_screener.cli $command >> `"$log`" 2>&1"
    return $LASTEXITCODE
}

$code = Step "refresh"
if ($code -ne 0) { exit $code }
$code = Step "track"
if ($code -eq 0) { Set-Content $marker $today.ToString("yyyy-MM-dd") }
# Export and coverage are conveniences: still run them if tracking failed, but report it.
Step "export" | Out-Null
Step "coverage" | Out-Null
exit $code
