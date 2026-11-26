@echo off
REM ===================================================================
REM  Runs one collection pass.
REM
REM  API keys are read from environment variables. Do NOT paste keys
REM  into this file. Set them once in Windows:
REM     setx BRAVE_API_KEY "your-key"
REM     setx ANTHROPIC_API_KEY "your-key"
REM  then open a NEW terminal / double-click this file again.
REM ===================================================================

set WORKBOOK=consensus.xlsx

cd /d "%~dp0"

if "%BRAVE_API_KEY%"=="" (
    echo.
    echo   BRAVE_API_KEY is not set. See the comments at the top of run.bat.
    echo.
    pause
    exit /b 1
)
if "%ANTHROPIC_API_KEY%"=="" (
    echo.
    echo   ANTHROPIC_API_KEY is not set. See the comments at the top of run.bat.
    echo.
    pause
    exit /b 1
)

python collect.py --workbook "%WORKBOOK%" %*

echo.
echo   Done. Open %WORKBOOK% and look at the `pending` sheet.
echo.
pause
