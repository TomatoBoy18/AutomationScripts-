@echo off
setlocal
cd /d "%~dp0"

echo ==== setup: create virtual env and install dependencies ====
echo.

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if not %ERRORLEVEL%==0 (
    echo [ERROR] Python not found in PATH. Install Python 3.9+ first.
    pause
    exit /b 1
  )
  set "PY=python"
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [1/2] creating .venv ...
  %PY% -m venv "%~dp0.venv"
) else (
  echo [1/2] .venv already exists, skipping.
)

echo [2/2] installing requirements ...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
set RC=%ERRORLEVEL%

if not exist "%~dp0config.json" (
  echo.
  echo writing config.json from the built-in defaults ...
  "%~dp0.venv\Scripts\python.exe" "%~dp0typing_auto.py" --init-config
  echo Edit config.json: set your site URLs, selectors and speed range.
)

echo.
echo Finished. Exit code = %RC%
echo Next step: edit config.json, then double-click run.bat
pause
exit /b %RC%
