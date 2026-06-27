@echo off
title Gemini Proxy Server

cd /d "%~dp0"

REM Sanity check: python must be on PATH.
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python is not on PATH. Install Python 3.10+ and retry.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env not found. Run config_tool.py first.
    pause
    exit /b 1
)

REM Read PORT from .env (empty if missing).
set PORT=
for /f "tokens=2 delims==" %%a in ('findstr "^PORT=" .env') do set PORT=%%a
if "%PORT%"=="" set PORT=1800

echo Starting proxy on port %PORT%...
echo (Change PORT= in .env to use a different port)
echo Press Ctrl+C to stop.
echo.

python -u server.py %PORT%

if errorlevel 1 (
    echo.
    echo [ERROR] Server failed to start (port %PORT% maybe in use).
    pause
)
