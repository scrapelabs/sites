@echo off
REM ============================================================================
REM  Restart the Permitlify app service - the easy button.
REM
REM  RIGHT-CLICK -> "Run as administrator".
REM ============================================================================
setlocal

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please RIGHT-CLICK this file and choose "Run as administrator".
    pause & exit /b 1
)

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "NSSM=%PROJECT_DIR%\nssm.exe"

echo Restarting the Permitlify service ...
if exist "%NSSM%" (
    "%NSSM%" restart Permitlify
) else (
    net stop Permitlify
    net start Permitlify
)
if errorlevel 1 (
    echo.
    echo [ERROR] Restart failed. Is the service named "Permitlify" installed?
    echo         Check Services ^(services.msc^) or run setup_windows.bat.
    pause & exit /b 1
)

echo.
echo ============================================================================
echo  Done - Permitlify restarted.
echo  Test it:  curl -s -o NUL -w "%%{time_total}s\n" -H "X-Forwarded-Proto: https" http://127.0.0.1/
echo ============================================================================
pause
endlocal
