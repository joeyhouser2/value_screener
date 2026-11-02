# Weekly refresh for Windows Task Scheduler.
#
# Register once (runs Saturdays 07:00):
#   $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
#              -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\scripts\weekly_refresh.ps1`""
#   $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At 7am
#   $options = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
#   Register-ScheduledTask -TaskName "value_screener weekly" -Action $action -Trigger $trigger -Settings $options
#
# A missed Saturday (PC asleep or off) runs at the next chance. It runs while you're logged on.
#
# Logs go to data\logs\refresh_YYYY-MM-DD.log.

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("refresh_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

$python = Join-Path $repo ".venv\Scripts\python.exe"
& $python -m value_screener.cli refresh *>&1 | Tee-Object -FilePath $log
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m value_screener.cli track *>&1 | Tee-Object -FilePath $log -Append
& $python -m value_screener.cli export *>&1 | Tee-Object -FilePath $log -Append
& $python -m value_screener.cli coverage *>&1 | Tee-Object -FilePath $log -Append
