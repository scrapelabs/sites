@echo off
REM ============================================================================
REM  Install Caddy as the port-80 reverse proxy (the front door for ALL sites).
REM
REM  WHAT THIS DOES (run ONCE; re-run only to refresh caddy.exe / re-register):
REM    1) Downloads caddy.exe (if missing).
REM    2) VALIDATES the Caddyfile BEFORE changing anything.
REM    3) Frees port 80 (stops IIS if it happens to be holding it).
REM    4) Registers + starts an auto-start "Caddy" Windows service on port 80.
REM    5) Health-checks that Caddy is answering on port 80.
REM
REM  This script ONLY sets up the front door. It does NOT start your sites -- run
REM  new_site.bat for each site (Permitlify, GoldenProxies, ...) on its own port.
REM  Caddy then routes each domain to its site per the Caddyfile.
REM
REM  RIGHT-CLICK -> "Run as administrator".
REM  Prerequisite: nssm.exe sits in THIS 0_setup folder (download from nssm.cc).
REM
REM  SECURITY: the apps trust X-Forwarded-* from any caller, so your server's
REM  firewall MUST allow inbound port 80 from Cloudflare's IP ranges ONLY.
REM  Otherwise someone hitting the raw IP could spoof the HTTPS header.
REM ============================================================================
setlocal
set "KIT_DIR=%~dp0"
if "%KIT_DIR:~-1%"=="\" set "KIT_DIR=%KIT_DIR:~0,-1%"
set "NSSM=%KIT_DIR%\nssm.exe"
set "CADDY=%KIT_DIR%\caddy.exe"
set "CADDYFILE=%KIT_DIR%\Caddyfile"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please RIGHT-CLICK this file and choose "Run as administrator".
    pause & exit /b 1
)
if not exist "%NSSM%" (
    echo [ERROR] nssm.exe not found at "%NSSM%". Put nssm.exe in this 0_setup folder.
    pause & exit /b 1
)

echo.
echo [1/5] Fetching caddy.exe (if missing)...
if not exist "%CADDY%" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop';" ^
      "Invoke-WebRequest -Uri 'https://caddyserver.com/api/download?os=windows&arch=amd64' -OutFile '%CADDY%'"
)
if not exist "%CADDY%" (
    echo [ERROR] Could not download Caddy. Check the server's internet connection.
    pause & exit /b 1
)

echo.
echo [2/5] Validating the Caddyfile BEFORE changing anything...
"%CADDY%" validate --config "%CADDYFILE%" --adapter caddyfile
if errorlevel 1 (
    echo [ERROR] Caddyfile is invalid - see the messages above. Nothing was
    echo         changed. Fix the Caddyfile and re-run.
    pause & exit /b 1
)

echo.
echo [3/5] Freeing port 80 (stopping IIS if it is running)...
net stop W3SVC >nul 2>&1

echo.
echo [4/5] Installing / starting the "Caddy" service on port 80...
"%NSSM%" stop Caddy >nul 2>&1
"%NSSM%" remove Caddy confirm >nul 2>&1
"%NSSM%" install Caddy "%CADDY%" run --config "%CADDYFILE%" --adapter caddyfile
if errorlevel 1 (
    echo [ERROR] Could not register the Caddy service. See messages above.
    pause & exit /b 1
)
"%NSSM%" set Caddy AppDirectory "%KIT_DIR%"
"%NSSM%" set Caddy Start SERVICE_AUTO_START
"%NSSM%" set Caddy AppStdout "%KIT_DIR%\caddy.out.log"
"%NSSM%" set Caddy AppStderr "%KIT_DIR%\caddy.err.log"
"%NSSM%" start Caddy
if errorlevel 1 (
    echo [ERROR] Caddy service failed to start. Review caddy.err.log in this folder.
    pause & exit /b 1
)

echo.
echo [5/5] Health-checking the new front door on port 80...
timeout /t 3 /nobreak >nul
curl -s -o NUL http://127.0.0.1
if errorlevel 1 (
    echo [ERROR] Port 80 is not answering. Is something else holding it?
    echo         Review caddy.err.log in this folder, then re-run.
    "%NSSM%" stop Caddy >nul 2>&1
    pause & exit /b 1
)

echo.
echo ============================================================================
echo  SUCCESS. Caddy is the front door on port 80 and routes by domain (Caddyfile).
echo  A site shows 502 until you start it -- that is expected. Next:
echo.
echo    new_site.bat ^<Service^> ^<ProjectDir^> ^<Port^>     (start each site)
echo    then add a Caddyfile block + run reload_caddy.bat   (if not already there)
echo.
echo  Reminder: Caddy owns port 80 now -- do NOT bind any app directly to :80.
echo ============================================================================
pause
endlocal
