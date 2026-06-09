@echo off
REM ============================================================================
REM  Permitlify - rebuild a CLEAN Python 3.12 virtualenv and install everything.
REM
REM  Use this when moving Python versions or when you see driver errors like
REM  "no pq wrapper available / cannot import name 'pq' from psycopg_binary".
REM  It DELETES the old .venv and rebuilds it fresh so there are no leftover
REM  packages from a previous Python version.
REM
REM  Prerequisite: Python 3.12 installed and on PATH (https://www.python.org/).
REM  RIGHT-CLICK -> "Run as administrator" (needed to stop/start the service).
REM ============================================================================
setlocal
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "VENV_PY=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "NSSM=%PROJECT_DIR%\nssm.exe"

cd /d "%PROJECT_DIR%" || (echo [ERROR] Cannot enter %PROJECT_DIR% & pause & exit /b 1)

echo.
echo [1/5] Stopping the Permitlify service (so its files can be replaced) ...
if exist "%NSSM%" "%NSSM%" stop Permitlify >nul 2>&1

echo.
echo [2/5] Removing any old virtual environment for a clean rebuild ...
if exist "%PROJECT_DIR%\.venv" rmdir /s /q "%PROJECT_DIR%\.venv"
if exist "%PROJECT_DIR%\.venv" (
    echo [ERROR] Could not delete the old .venv - a program is still using it.
    echo         Stop the "Permitlify" service in Services, then re-run this as administrator.
    pause & exit /b 1
)

echo.
echo [3/5] Creating a fresh Python 3.12 virtual environment ...
where py >nul 2>&1
if %errorlevel%==0 (
    py -3.12 -m venv .venv
) else (
    python -m venv .venv
)
if not exist "%VENV_PY%" (
    echo [ERROR] Could not create the virtual environment.
    echo         Make sure Python 3.12 is installed and on PATH ^(run: py -3.12 --version^).
    pause & exit /b 1
)

echo.
echo [4/5] Installing all requirements (this can take a few minutes) ...
"%VENV_PY%" --version
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Dependency install failed - see the messages above.
    pause & exit /b 1
)

echo.
echo [5/5] Verifying the database driver loads, then restarting the app ...
"%VENV_PY%" -c "import psycopg; print('psycopg', psycopg.__version__, 'loaded OK')"
if errorlevel 1 (
    echo [ERROR] psycopg still fails to import - copy the message above and send it to me.
    pause & exit /b 1
)
if exist "%NSSM%" "%NSSM%" restart Permitlify

echo.
echo ============================================================================
echo  Clean .venv (Python 3.12) built, all requirements installed and verified.
echo  The Permitlify service has been restarted - reload your site.
echo ============================================================================
pause
endlocal
