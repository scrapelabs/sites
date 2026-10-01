@echo off
setlocal
title llama.cpp GPT-OSS Server

cd /d "%~dp0"

set GGUF_FILE=models\gpt-oss-20b-mxfp4.gguf
set PORT=8010
if not defined N_THREADS set N_THREADS=4

echo.
echo ========================================
echo   llama.cpp GPT-OSS API Server
echo ========================================
echo.
echo Model:   %GGUF_FILE%
echo Port:    %PORT%
echo Threads: %N_THREADS%
echo.

if not exist "%GGUF_FILE%" (
    echo ERROR: Model file not found at %GGUF_FILE%
    echo.
    echo Download it first:
    echo   hf download ggml-org/gpt-oss-20b-GGUF --include "*.gguf" --local-dir models
    pause
    exit /b 1
)

echo Starting server...
echo.

.\llama\llama-server.exe ^
    -m "%GGUF_FILE%" ^
    --host 127.0.0.1 ^
    --port %PORT% ^
    --threads %N_THREADS% ^
    --ctx-size 8192 ^
    --alias gpt-oss-20b ^
    --jinja

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Server exited with error code %ERRORLEVEL%
    pause
)
