@echo off
REM ============================================================================
REM  Apply Caddyfile changes with ZERO downtime.
REM  Run this after you edit Caddyfile (e.g. added a new site block).
REM  RIGHT-CLICK -> "Run as administrator".
REM ============================================================================
setlocal
set "KIT_DIR=%~dp0"
if "%KIT_DIR:~-1%"=="\" set "KIT_DIR=%KIT_DIR:~0,-1%"
set "CADDY=%KIT_DIR%\caddy.exe"
set "CADDYFILE=%KIT_DIR%\Caddyfile"

if not exist "%CADDY%" (
    echo [ERROR] caddy.exe is missing. Run setup_caddy.bat first.
    pause & exit /b 1
)

echo Validating Caddyfile...
"%CADDY%" validate --config "%CADDYFILE%" --adapter caddyfile
if errorlevel 1 (
    echo.
    echo [ERROR] Caddyfile has errors - see above. NOT reloading; old config stays live.
    pause & exit /b 1
)

echo.
echo Reloading Caddy (no downtime)...
"%CADDY%" reload --config "%CADDYFILE%" --adapter caddyfile --address localhost:2019
if errorlevel 1 (
    echo [ERROR] Reload failed. Is the Caddy service running? Try: nssm restart Caddy
    pause & exit /b 1
)

echo.
echo ============================================================================
echo  Done. The new routing is live.
echo ============================================================================
pause
endlocal
