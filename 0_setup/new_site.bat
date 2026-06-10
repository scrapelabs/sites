@echo off
REM ============================================================================
REM  Add a NEW website as its own auto-start Windows service on its own
REM  private loopback port. This sets up a Python/Django app served by waitress
REM  the SAME way Permitlify runs. (For a non-Python app, see the NOTE below.)
REM
REM  USAGE (RIGHT-CLICK -> "Run as administrator", or from an admin prompt):
REM     new_site.bat <ServiceName> <ProjectDir> <Port>
REM  EXAMPLE:
REM     new_site.bat Site2 C:\sites\site2 8001
REM
REM  Pick a UNIQUE port per site (8001, 8002, 8003 ...). Permitlify uses 8000.
REM
REM  AFTER this finishes:
REM    1) Add a block to Caddyfile mapping your domain -> 127.0.0.1:<Port>
REM    2) reload_caddy.bat
REM    3) Cloudflare: add the domain, A-record -> this server's IP, proxied (orange).
REM ============================================================================
setlocal
set "KIT_DIR=%~dp0"
if "%KIT_DIR:~-1%"=="\" set "KIT_DIR=%KIT_DIR:~0,-1%"
set "NSSM=%KIT_DIR%\nssm.exe"

set "SVC=%~1"
set "DIR=%~2"
set "PORT=%~3"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please RIGHT-CLICK this file and choose "Run as administrator".
    pause & exit /b 1
)
if "%SVC%"=="" goto :usage
if "%DIR%"=="" goto :usage
if "%PORT%"=="" goto :usage
if not exist "%NSSM%" (
    echo [ERROR] nssm.exe not found at "%NSSM%". Put nssm.exe in the 0_setup folder.
    pause & exit /b 1
)
if not exist "%DIR%" (
    echo [ERROR] Project dir "%DIR%" does not exist.
    pause & exit /b 1
)

set "VENV_PY=%DIR%\.venv\Scripts\python.exe"

echo.
echo [1/4] Creating virtual environment in "%DIR%\.venv" ...
where py >nul 2>&1
if %errorlevel%==0 (
    py -3.12 -m venv "%DIR%\.venv" 2>nul || py -m venv "%DIR%\.venv"
) else (
    python -m venv "%DIR%\.venv"
)
if not exist "%VENV_PY%" (
    echo [ERROR] Could not create the virtual environment. Is Python 3.12 on PATH?
    pause & exit /b 1
)

echo.
echo [2/4] Installing requirements (plus waitress) ...
"%VENV_PY%" -m pip install --upgrade pip
if exist "%DIR%\requirements.txt" (
    "%VENV_PY%" -m pip install -r "%DIR%\requirements.txt"
) else (
    echo [WARN] No requirements.txt in "%DIR%" - skipping dependency install.
)
REM waitress is the WSGI server that actually runs the site - make sure it is in.
"%VENV_PY%" -m pip install waitress

REM If the project has no launcher of its own, drop in the generic one. It works
REM with any Django project and auto-detects the settings module from manage.py.
if not exist "%DIR%\serve_waitress.py" (
    echo [INFO] No serve_waitress.py found - copying the generic launcher in.
    copy /Y "%KIT_DIR%\serve_waitress.py" "%DIR%\serve_waitress.py" >nul
)

echo.
echo [3/4] Registering the "%SVC%" service on 127.0.0.1:%PORT% ...
"%NSSM%" stop %SVC% >nul 2>&1
"%NSSM%" remove %SVC% confirm >nul 2>&1
"%NSSM%" install %SVC% "%VENV_PY%" "%DIR%\serve_waitress.py"
"%NSSM%" set %SVC% AppDirectory "%DIR%"
"%NSSM%" set %SVC% AppEnvironmentExtra "HOST=127.0.0.1" "PORT=%PORT%"
"%NSSM%" set %SVC% Start SERVICE_AUTO_START
if not exist "%DIR%\logs" mkdir "%DIR%\logs"
"%NSSM%" set %SVC% AppStdout "%DIR%\logs\waitress.out.log"
"%NSSM%" set %SVC% AppStderr "%DIR%\logs\waitress.err.log"

echo.
echo [4/4] Starting %SVC% ...
"%NSSM%" start %SVC%

echo.
echo ============================================================================
echo  Done. "%SVC%" serves on http://127.0.0.1:%PORT% and auto-starts on boot.
echo.
echo  NEXT STEPS:
echo    1) Open Caddyfile and add (change YOURDOMAIN):
echo         http://YOURDOMAIN.com, http://www.YOURDOMAIN.com {
echo             reverse_proxy 127.0.0.1:%PORT% { header_up X-Forwarded-Proto https }
echo         }
echo    2) Run reload_caddy.bat
echo    3) Cloudflare: add the domain, A-record -> this server's IP, orange-cloud ON.
echo.
echo  Each site needs its OWN .env (own DJANGO_SECRET_KEY + its own database).
echo.
echo  NOTE: this runs a Django site with waitress. If the project had no
echo  serve_waitress.py, a generic one was copied in for you. For a non-Django
echo  stack run:  nssm edit %SVC%  and point Application + Arguments at that
echo  app's own start command, keeping the port at %PORT%.
echo ============================================================================
pause
endlocal
goto :eof

:usage
echo USAGE:  new_site.bat ^<ServiceName^> ^<ProjectDir^> ^<Port^>
echo EXAMPLE: new_site.bat Site2 C:\sites\site2 8001
pause & exit /b 1
