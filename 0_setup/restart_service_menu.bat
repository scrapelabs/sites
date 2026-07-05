@echo off
setlocal EnableExtensions

set "NSSM=%~dp0nssm.exe"

if not exist "%NSSM%" (
  echo ERROR: nssm.exe was not found next to this script.
  echo Expected: %NSSM%
  pause
  exit /b 1
)

:menu
cls
echo Restart a service
echo =================
echo.
echo  1. Permitlify
echo  2. GoldenProxies
echo  3. Caddy
echo  4. GptOss20B
echo  5. Show service status
echo  0. Exit
echo.
set /p "choice=Select a number: "

if "%choice%"=="" exit /b 0
if "%choice%"=="1" set "SERVICE=Permitlify"& goto restart
if "%choice%"=="2" set "SERVICE=GoldenProxies"& goto restart
if "%choice%"=="3" set "SERVICE=Caddy"& goto restart
if "%choice%"=="4" set "SERVICE=GptOss20B"& goto restart
if "%choice%"=="5" goto status
if "%choice%"=="0" exit /b 0

echo.
echo Invalid selection.
pause
goto menu

:restart
echo.
echo Restarting %SERVICE%...
"%NSSM%" restart "%SERVICE%"
echo.
"%NSSM%" status "%SERVICE%"
echo.
pause
goto menu

:status
echo.
echo Service status
echo --------------
for %%S in (Permitlify GoldenProxies Caddy GptOss20B) do (
  echo %%S:
  "%NSSM%" status "%%S"
  echo.
)
pause
goto menu
