@echo off
REM Reddit ticker mention snapshot sync — Windows Task Scheduler entry.
REM 가변 주기: peak (KST 22:30~05:00) 15분, off 1시간. 스크립트 내부 게이트로 skip 결정.

cd /d "C:\Users\jst75\pro\투자정보"

if not exist "logs" mkdir "logs"

REM PowerShell 로 ISO yyyyMMdd 추출 후 append
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "TODAY=%%i"
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm:ss\""') do set "STAMP=%%i"

set "LOGFILE=logs\reddit_sync_%TODAY%.log"

echo. >> "%LOGFILE%"
echo === %STAMP% === >> "%LOGFILE%"
"C:\Users\jst75\AppData\Local\Programs\Python\Python311\python.exe" scripts\sync_us_reddit_mentions.py >> "%LOGFILE%" 2>&1
