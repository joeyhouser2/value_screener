# Creates a "Value Screener" shortcut on the Desktop that launches the dashboard.
$root = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Value Screener.lnk'

$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = Join-Path $root 'scripts\launch_dashboard.bat'
$s.WorkingDirectory = $root
$s.IconLocation = (Join-Path $root 'assets\icon.ico') + ',0'
$s.WindowStyle = 7   # minimized console
$s.Description = 'Industry-relative value screener dashboard'
$s.Save()
Write-Host "Created $lnk"
