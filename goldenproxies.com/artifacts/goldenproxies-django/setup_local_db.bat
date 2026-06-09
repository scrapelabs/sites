@echo off
REM ============================================================
REM  GoldenProxies - Local database setup (Windows)
REM ------------------------------------------------------------
REM  Creates the local SQLite database (db.sqlite3) with ALL
REM  tables, the super admin account, and restores any persisted
REM  data from the TinyDB snapshot.
REM
REM  Just double-click this file, or run it from a terminal:
REM      setup_local_db.bat
REM ============================================================

cd /d "%~dp0"

echo.
echo ============================================
echo   GoldenProxies - local database setup
echo ============================================
echo.

REM --- 1. Locate a Python interpreter -------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python was not found on your PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

REM --- 2. Create / reuse a virtual environment ----------------
if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat"

REM --- 3. Install dependencies --------------------------------
echo Installing dependencies from requirements.txt ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

REM --- 4. Build the database schema (all tables) --------------
echo.
echo Generating migrations ...
python manage.py makemigrations core --noinput

echo Applying migrations (creates all tables) ...
python manage.py migrate --noinput
if errorlevel 1 (
    echo [ERROR] Migration failed - database was not created.
    pause
    exit /b 1
)

REM --- 4b. Collect static files (needed by Waitress/Whitenoise)
echo.
echo Collecting static files ...
python manage.py collectstatic --noinput

REM --- 5. Ensure the super admin account ----------------------
echo.
echo Ensuring super admin account ...
python manage.py shell -c "from django.contrib.auth.models import User; email='khemiri.mohamed.ensi@gmail.com'; (User.objects.filter(email=email).exists() or User.objects.create_superuser(username=email, email=email, password='admin123', first_name='Admin', last_name='GoldenProxies')) and None; print('Super admin ready:', email)"

REM --- 6. Restore persisted data (optional) -------------------
if exist "scripts\restore_from_tinydb.py" (
    echo.
    echo Restoring data from TinyDB snapshot ...
    python scripts\restore_from_tinydb.py
)

echo.
echo ============================================
echo   Done! db.sqlite3 is ready with all tables.
echo ============================================
echo.
echo Start the server with:
echo     .venv\Scripts\activate
echo     python manage.py runserver
echo.
echo Then open http://127.0.0.1:8000/
echo Admin login: khemiri.mohamed.ensi@gmail.com / admin123
echo.
pause
