@echo off
REM Compatibility entry point; keep one source of launcher configuration.
call "%~dp0start_server.bat" %*
exit /b %ERRORLEVEL%
