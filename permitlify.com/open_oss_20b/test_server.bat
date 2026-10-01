@echo off
setlocal
title GPT-OSS API Test
cd /d "%~dp0"
if errorlevel 1 exit /b 1
python test.py %*
set "test_exit_code=%errorlevel%"
echo.
if defined PERMITLIFY_AI_PAUSE pause
exit /b %test_exit_code%
