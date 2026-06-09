@echo off
REM ============================================================================
REM  Permitlify - IIS HTTPS front door (reverse proxy to waitress)
REM
REM  Installs/enables IIS + URL Rewrite + ARR, turns on the reverse proxy,
REM  allows the X-Forwarded-Proto header, and creates the public web site.
REM
REM  RIGHT-CLICK this file -> "Run as administrator".
REM  Run setup_windows.bat FIRST (so waitress is already serving on :8000).
REM
REM  After this finishes, install the free HTTPS certificate with win-acme:
REM    download wacs.exe from https://www.win-acme.com , run it, pick this site.
REM ============================================================================

setlocal enabledelayedexpansion

REM --- Edit this if your domain differs --------------------------------------
set "DOMAIN=permitdaily.com"

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "SITE_NAME=Permitlify"
set "APPCMD=%windir%\system32\inetsrv\appcmd.exe"

REM --- Must be Administrator --------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please RIGHT-CLICK this file and choose "Run as administrator".
    pause
    exit /b 1
)

echo.
echo [1/6] Enabling IIS (this can take a minute)...
dism /online /enable-feature /featurename:IIS-WebServerRole /all /norestart >nul

echo.
echo [2/6] Installing URL Rewrite module...
if not exist "%TEMP%\rewrite.msi" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop'; Invoke-WebRequest -Uri 'https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi' -OutFile '%TEMP%\rewrite.msi'" 2>nul
)
if exist "%TEMP%\rewrite.msi" (
    msiexec /i "%TEMP%\rewrite.msi" /quiet /norestart
) else (
    echo [WARN] Could not download URL Rewrite. Install it manually from:
    echo        https://www.iis.net/downloads/microsoft/url-rewrite
)

echo.
echo [3/6] Installing Application Request Routing (ARR)...
if not exist "%TEMP%\arr.msi" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop'; Invoke-WebRequest -Uri 'https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi' -OutFile '%TEMP%\arr.msi'" 2>nul
)
if exist "%TEMP%\arr.msi" (
    msiexec /i "%TEMP%\arr.msi" /quiet /norestart
) else (
    echo [WARN] Could not download ARR. Install it manually from:
    echo        https://www.iis.net/downloads/microsoft/application-request-routing
)

echo.
echo [4/6] Enabling the reverse proxy + allowing X-Forwarded-Proto...
"%APPCMD%" set config -section:system.webServer/proxy /enabled:"true" /commit:apphost
if %errorlevel% neq 0 (
    echo [WARN] Could not enable the proxy - ARR may not be installed yet.
    echo        Install ARR, then re-run this script.
)
"%APPCMD%" set config -section:system.webServer/rewrite/allowedServerVariables /+"[name='HTTP_X_FORWARDED_PROTO']" /commit:apphost >nul 2>&1

echo.
echo [5/6] Creating the "%SITE_NAME%" web site for %DOMAIN%...
"%APPCMD%" add site /name:"%SITE_NAME%" /physicalPath:"%PROJECT_DIR%" /bindings:"http/*:80:%DOMAIN%" >nul 2>&1
"%APPCMD%" set site /site.name:"%SITE_NAME%" "/+bindings.[protocol='http',bindingInformation='*:80:www.%DOMAIN%']" >nul 2>&1
echo     (web.config in this folder provides the reverse-proxy rule.)

echo.
echo [6/6] Restarting IIS...
iisreset >nul

echo.
echo ============================================================================
echo  Done. IIS now proxies http://%DOMAIN% to waitress on 127.0.0.1:8000.
echo.
echo  LAST STEP - turn on HTTPS (required, the app forces it):
echo    1) Download wacs.exe from https://www.win-acme.com
echo    2) Run it as administrator, choose "%DOMAIN%" / the Permitlify site
echo    3) It installs a free Let's Encrypt certificate and auto-renews it.
echo.
echo  Also make sure your domain's DNS A records point %DOMAIN% (and www) at
echo  this server's public IP.
echo ============================================================================
pause
endlocal
