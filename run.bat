@echo off
setlocal
cd /d "%~dp0"

set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
  echo [WARN] .venv not found, falling back to the python on PATH.
  echo        Run setup.bat once to create a local virtual env.
  set "PYEXE=python"
)

if not exist "%~dp0typing_auto.py" (
  echo [ERROR] typing_auto.py not found in %~dp0
  pause
  exit /b 1
)

if not exist "%~dp0config.json" (
  echo [HINT] config.json not found, using built-in defaults.
  echo        Run setup.bat or: python typing_auto.py --init-config
  echo.
)

echo ==== web typing automation ====
echo.
"%PYEXE%" "%~dp0typing_auto.py" %*
set RC=%ERRORLEVEL%

echo.
echo Finished. Exit code = %RC%
pause
exit /b %RC%
