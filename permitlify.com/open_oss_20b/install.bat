@echo off
title GPT-OSS Setup

cd /d "%~dp0"

echo ========================================
echo   GPT-OSS - Installing Requirements
echo ========================================
echo.

REM === Step 1: Check Python ===
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [FAIL] Python is not installed.
    echo        Download from: https://www.python.org/downloads/
    echo        Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PY_VER=%%i
echo [OK]   Python %PY_VER%

REM === Step 2: Install Python packages ===
echo.
echo [....] Installing Python packages (requests, huggingface-hub)...
pip install requests huggingface-hub 2>&1 | findstr /V "Requirement already satisfied" >nul
if %ERRORLEVEL% NEQ 0 (
    echo [OK]   Packages installed
) else (
    echo [OK]   Packages already installed
)

REM === Step 3: Download llama-server if missing ===
echo.
if not exist "llama\llama-server.exe" (
    echo [....] Downloading llama-server binary...
    if not exist "llama" mkdir llama
    curl -L -o "%TEMP%\llama.zip" "https://github.com/ggml-org/llama.cpp/releases/download/b9870/llama-b9870-bin-win-cpu-x64.zip"
    powershell -Command "Expand-Archive -Path '%TEMP%\llama.zip' -DestinationPath 'llama' -Force"
    del "%TEMP%\llama.zip" 2>nul
    echo [OK]   llama-server downloaded
) else (
    echo [OK]   llama-server already exists
)

REM === Step 4: Download model if missing ===
echo.
if not exist "models\gpt-oss-20b-mxfp4.gguf" (
    echo [....] Downloading model (12 GB, may take a while)...
    if not exist "models" mkdir models
    hf download ggml-org/gpt-oss-20b-GGUF --include "*.gguf" --local-dir models
    if %ERRORLEVEL% NEQ 0 (
        echo [FAIL] Model download failed.
        echo        Try manually: hf download ggml-org/gpt-oss-20b-GGUF --include "*.gguf" --local-dir models
        pause
        exit /b 1
    )
    echo [OK]   Model downloaded
) else (
    echo [OK]   Model already exists
)

REM === Done ===
echo.
echo ========================================
echo   Setup complete!
echo ========================================
echo.
echo   Start server:  double-click start_server.bat
echo   Test API:      double-click test_server.bat
echo.
pause
