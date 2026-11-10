@echo off
rem Starts the Value Screener dashboard (or just opens it if already running).
cd /d "%~dp0.."
set PORT=8501

netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul
if %errorlevel%==0 (
    start "" "http://localhost:%PORT%"
    exit /b 0
)

rem Headless skips Streamlit's first-run email prompt; we open the browser ourselves
rem once the server is listening.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i -lt 60;$i++){ try{ Invoke-WebRequest http://localhost:%PORT% -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process http://localhost:%PORT%; break }catch{ Start-Sleep 1 } }"

title Value Screener (close this window to stop the dashboard)
".venv\Scripts\python.exe" -m streamlit run app.py --server.port %PORT% --server.headless true --browser.gatherUsageStats false
