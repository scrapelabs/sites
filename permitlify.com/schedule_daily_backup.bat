@echo off
REM ============================================================================
REM  Schedule the local-database backup to run automatically every day at 03:00.
REM  RIGHT-CLICK -> "Run as administrator".  Run this ONCE.
REM ============================================================================
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please RIGHT-CLICK this file and choose "Run as administrator".
    pause & exit /b 1
)

set "HERE=%~dp0"
schtasks /Create /TN "PermitlifyBackup" /TR "\"%HERE%backup_local_db.bat\"" /SC DAILY /ST 03:00 /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 (
    echo [ERROR] Could not create the scheduled task.
    pause & exit /b 1
)

echo.
echo ============================================================================
echo  Scheduled: a DAILY backup at 03:00 -> C:\permitlify-backups (keeps 14 days).
echo  Test it now by running:  backup_local_db.bat
echo ============================================================================
pause
endlocal
