@echo off
REM ============================================================================
REM  Install Caddy as the port-80 reverse proxy (the front door for ALL sites).
REM
REM  WHAT THIS DOES (run ONCE; re-run only to refresh caddy.exe / re-register):
REM    1) Downloads caddy.exe (if missing).
REM    2) VALIDATES the Caddyfile BEFORE changing anything.
REM    3) Moves the Permitlify app OFF port 80, back to loopback 127.0.0.1:8000
REM       (Caddy now owns port 80 and forwards to it). After this you no longer
REM       use run_on_port80.bat -- Caddy is the front door.
REM    4) Stops IIS if it happens to be holding port 80.
REM    5) Registers + starts an auto-start "Caddy" Windows service.
REM    6) Health-checks the new front door, and AUTO-ROLLS-BACK Permitlify to
REM       port 80 if anything fails -- so the live site is never left down.
REM
REM  RIGHT-CLICK -> "Run as administrator".
REM  Prerequisite: you already ran setup_windows.bat (so nssm.exe exists).
REM
REM  SECURITY: the apps trust X-Forwarded-* from any caller, so your server's
REM  firewall MUST allow inbound port 80 from Cloudflare's IP ranges ONLY.
REM  Otherwise someone hitting the raw IP could spoof the HTTPS header.
REM ============================================================================
setlocal
set "KIT_DIR=%~dp0"
if "%KIT_DIR:~-1%"=="\" set "KIT_DIR=%KIT_DIR:~0,-1%"
set "ROOT=%KIT_DIR%\.."
set "NSSM=%ROOT%\nssm.exe"
set "CADDY=%KIT_DIR%\caddy.exe"
set "CADDYFILE=%KIT_DIR%\Caddyfile"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please RIGHT-CLICK this file and choose "Run as administrator".
    pause & exit /b 1
)
if not exist "%NSSM%" (
    echo [ERROR] nssm.exe not found at "%NSSM%". Run setup_windows.bat first.
    pause & exit /b 1
)

echo.
echo [1/6] Fetching caddy.exe (if missing)...
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
echo [2/6] Validating the Caddyfile BEFORE touching the running site...
"%CADDY%" validate --config "%CADDYFILE%" --adapter caddyfile
if errorlevel 1 (
    echo [ERROR] Caddyfile is invalid - see the messages above. Nothing was
    echo         changed; the site is still running. Fix the Caddyfile and re-run.
    pause & exit /b 1
)

echo.
echo [3/6] Moving Permitlify back to loopback 127.0.0.1:8000 (Caddy will own :80)...
"%NSSM%" set Permitlify AppEnvironmentExtra "HOST=127.0.0.1" "PORT=8000"
"%NSSM%" restart Permitlify
REM Give waitress a moment to bind, then confirm it is listening on 8000.
timeout /t 3 /nobreak >nul
curl -s -o NUL -H "Host: permitdaily.com" -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/
if errorlevel 1 (
    echo [ERROR] Permitlify did not come up on 127.0.0.1:8000. Rolling back to port 80...
    goto :rollback
)

echo.
echo [4/6] Freeing port 80 (stopping IIS if it is running)...
net stop W3SVC >nul 2>&1

echo.
echo [5/6] Installing / starting the "Caddy" service on port 80...
"%NSSM%" stop Caddy >nul 2>&1
"%NSSM%" remove Caddy confirm >nul 2>&1
"%NSSM%" install Caddy "%CADDY%" run --config "%CADDYFILE%" --adapter caddyfile
if errorlevel 1 ( echo [ERROR] Could not register the Caddy service. Rolling back... & goto :rollback )
"%NSSM%" set Caddy AppDirectory "%KIT_DIR%"
"%NSSM%" set Caddy Start SERVICE_AUTO_START
"%NSSM%" set Caddy AppStdout "%KIT_DIR%\caddy.out.log"
"%NSSM%" set Caddy AppStderr "%KIT_DIR%\caddy.err.log"
"%NSSM%" start Caddy
if errorlevel 1 ( echo [ERROR] Caddy service failed to start. Rolling back... & goto :rollback )

echo.
echo [6/6] Health-checking the new front door on port 80...
timeout /t 3 /nobreak >nul
curl -s -o NUL -H "Host: permitdaily.com" http://127.0.0.1
if errorlevel 1 (
    echo [ERROR] Port 80 is not answering through Caddy. Rolling back to port 80...
    "%NSSM%" stop Caddy >nul 2>&1
    goto :rollback
)

echo.
echo ============================================================================
echo  SUCCESS. Caddy answers on port 80 and routes by domain (see Caddyfile).
echo  Permitlify is on 127.0.0.1:8000 behind it.
echo.
echo  Add more sites:  new_site.bat   (then add a Caddyfile block + reload_caddy.bat)
echo  Reminder: do NOT run run_on_port80.bat anymore -- Caddy owns port 80 now.
echo ============================================================================
pause
endlocal
goto :eof

:rollback
REM Restore the previous known-good state: Permitlify directly on port 80.
echo.
echo --- ROLLBACK: putting Permitlify back on 0.0.0.0:80 so the site stays up ---
"%NSSM%" stop Caddy >nul 2>&1
"%NSSM%" set Permitlify AppEnvironmentExtra "HOST=0.0.0.0" "PORT=80"
"%NSSM%" restart Permitlify
echo.
echo ============================================================================
echo  Rolled back. Permitlify is serving on port 80 again (no Caddy).
echo  Nothing else was changed. Review caddy.err.log in this folder, fix the
echo  problem, then re-run setup_caddy.bat.
echo ============================================================================
pause
endlocal
exit /b 1
