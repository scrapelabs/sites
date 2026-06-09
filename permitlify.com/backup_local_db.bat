@echo off
REM ============================================================================
REM  Back up the LOCAL database to a timestamped file.
REM
REM  IMPORTANT: now that the data lives on YOUR server, backups are YOUR job.
REM  Run schedule_daily_backup.bat once to make this run automatically every day.
REM  Keeps the newest 14 backups in C:\permitlify-backups.
REM ============================================================================
setlocal
call "%~dp0db_config.bat"

set "PGPASSWORD=%PG_SUPERPASS%"
set "BACKUP_DIR=C:\permitlify-backups"
set "DEST=postgresql://postgres@127.0.0.1:5432/%LOCAL_DB%"

if not exist "%PGBIN%\pg_dump.exe" (
    echo [ERROR] PostgreSQL 17 not found at %PGBIN%.
    exit /b 1
)
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%i"
set "OUT=%BACKUP_DIR%\permitlify_%STAMP%.dump"

echo Backing up "%LOCAL_DB%" to %OUT% ...
"%PGBIN%\pg_dump.exe" "%DEST%" -Fc -f "%OUT%"
if errorlevel 1 (
    echo [ERROR] Backup FAILED.
    exit /b 1
)

echo Pruning old backups (keeping the newest 14) ...
for /f "skip=14 delims=" %%f in ('dir /b /o-d "%BACKUP_DIR%\permitlify_*.dump" 2^>nul') do del "%BACKUP_DIR%\%%f"

echo Backup OK: %OUT%
endlocal
