@echo off
REM ============================================================================
REM  Permitlify - Windows one-shot setup
REM  Installs dependencies and registers waitress as an auto-start Windows
REM  service (runs forever, restarts on crash, starts on every boot).
REM
REM  Run this by RIGHT-CLICK -> "Run as administrator".
REM  Prerequisite: Python 3.12 installed and on PATH.
REM ============================================================================

setlocal enabledelayedexpansion

REM --- This script's own folder is the project dir (no hard-coded path) -------
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

set "SERVICE_NAME=Permitlify"
set "BIND_HOST=127.0.0.1"
set "BIND_PORT=8000"
set "VENV_PY=%PROJECT_DIR%\.venv\Scripts\python.exe"

cd /d "%PROJECT_DIR%" || (echo [ERROR] Cannot enter %PROJECT_DIR% & pause & exit /b 1)

REM --- Must be Administrator to install a service -----------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please RIGHT-CLICK this file and choose "Run as administrator".
    pause
    exit /b 1
)

echo.
echo [1/6] Creating virtual environment...
where py >nul 2>&1
if %errorlevel%==0 (
    py -3.12 -m venv .venv 2>nul || py -m venv .venv
) else (
    python -m venv .venv
)
if not exist "%VENV_PY%" (
    echo [ERROR] Could not create the virtual environment. Is Python 3.12 installed and on PATH?
    pause
    exit /b 1
)

echo.
echo [2/6] Installing dependencies (this can take a few minutes)...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Dependency install failed.
    pause
    exit /b 1
)

echo.
echo [3/6] Collecting static files...
"%VENV_PY%" manage.py collectstatic --noinput

echo.
echo [4/6] Fetching NSSM (the service manager)...
if not exist "%PROJECT_DIR%\nssm.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop';" ^
      "Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile 'nssm.zip';" ^
      "Expand-Archive -Path 'nssm.zip' -DestinationPath 'nssm_tmp' -Force;" ^
      "Copy-Item 'nssm_tmp\nssm-2.24\win64\nssm.exe' '.\nssm.exe' -Force;" ^
      "Remove-Item 'nssm.zip' -Force; Remove-Item 'nssm_tmp' -Recurse -Force"
)
if not exist "%PROJECT_DIR%\nssm.exe" (
    echo [ERROR] Could not download NSSM. Check the server's internet connection.
    pause
    exit /b 1
)

echo.
echo [5/6] Installing / updating the "%SERVICE_NAME%" service...
"%PROJECT_DIR%\nssm.exe" stop %SERVICE_NAME% >nul 2>&1
"%PROJECT_DIR%\nssm.exe" remove %SERVICE_NAME% confirm >nul 2>&1
"%PROJECT_DIR%\nssm.exe" install %SERVICE_NAME% "%VENV_PY%" "%PROJECT_DIR%\serve_waitress.py"
"%PROJECT_DIR%\nssm.exe" set %SERVICE_NAME% AppDirectory "%PROJECT_DIR%"
"%PROJECT_DIR%\nssm.exe" set %SERVICE_NAME% AppEnvironmentExtra "HOST=%BIND_HOST%" "PORT=%BIND_PORT%"
"%PROJECT_DIR%\nssm.exe" set %SERVICE_NAME% Start SERVICE_AUTO_START
"%PROJECT_DIR%\nssm.exe" set %SERVICE_NAME% AppStdout "%PROJECT_DIR%\logs\waitress.out.log"
"%PROJECT_DIR%\nssm.exe" set %SERVICE_NAME% AppStderr "%PROJECT_DIR%\logs\waitress.err.log"
"%PROJECT_DIR%\nssm.exe" set %SERVICE_NAME% AppStopMethodConsole 5000
if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

echo.
echo [6/6] Starting the service...
if not defined DJANGO_SECRET_KEY (
    echo [WARNING] DJANGO_SECRET_KEY is not set as a machine env var.
    echo           The service cannot start until you run set_env.bat as administrator.
)
"%PROJECT_DIR%\nssm.exe" start %SERVICE_NAME%

echo.
echo ============================================================================
echo  Done. Service "%SERVICE_NAME%" is installed and set to AUTO-START on boot.
echo  App serves on http://%BIND_HOST%:%BIND_PORT%  (loopback - put IIS in front for HTTPS)
echo.
echo  If you have NOT set your secrets yet:
echo    1) Edit set_env.bat with your values
echo    2) Run set_env.bat as administrator
echo    3) Re-run this file, or: nssm restart %SERVICE_NAME%
echo.
echo  Useful commands:
echo    nssm restart %SERVICE_NAME%     (apply new code/env)
echo    nssm stop    %SERVICE_NAME%
echo    logs are in: %PROJECT_DIR%\logs
echo ============================================================================
pause
endlocal
