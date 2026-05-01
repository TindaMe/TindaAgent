@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ---- stable Windows starter (ASCII only) ----
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

set "PORT_RETRIES=%~2"
if "%PORT_RETRIES%"=="" set "PORT_RETRIES=20"

set "PAUSE_ON_EXIT=%~3"
if "%PAUSE_ON_EXIT%"=="" set "PAUSE_ON_EXIT=1"

set "RELOAD=%~4"
if "%RELOAD%"=="" set "RELOAD=0"

echo.
echo [TindaAgent] starting...
echo [TindaAgent] workdir: %CD%
echo [TindaAgent] start port: %PORT%
echo [TindaAgent] port retries: %PORT_RETRIES%
if "%RELOAD%"=="1" (
  echo [TindaAgent] reload: on
) else (
  echo [TindaAgent] reload: off
)
echo.

REM Keep reload OFF by default.
REM Uvicorn reload mode runs watcher+worker processes; on terminal close, lingering child processes are more likely on Windows.

set "PY_EXE="
set "PY_PRE_ARGS="
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  set "PY_EXE=py"
  set "PY_PRE_ARGS=-3"
)
if "%PY_EXE%"=="" (
  where python >nul 2>nul
  if %ERRORLEVEL% EQU 0 set "PY_EXE=python"
)

REM Fallback 1: explicit env override
if "%PY_EXE%"=="" (
  if not "%TINDA_PYTHON%"=="" (
    if exist "%TINDA_PYTHON%" set "PY_EXE=%TINDA_PYTHON%"
  )
)

REM Fallback 2: common local virtual env
if "%PY_EXE%"=="" (
  if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
)
if "%PY_EXE%"=="" (
  if exist "%~dp0venv\Scripts\python.exe" set "PY_EXE=%~dp0venv\Scripts\python.exe"
)

REM Fallback 3: common conda/python install paths
if "%PY_EXE%"=="" (
  if exist "%USERPROFILE%\anaconda3\python.exe" set "PY_EXE=%USERPROFILE%\anaconda3\python.exe"
)
if "%PY_EXE%"=="" (
  if exist "%USERPROFILE%\miniconda3\python.exe" set "PY_EXE=%USERPROFILE%\miniconda3\python.exe"
)
if "%PY_EXE%"=="" (
  if exist "E:\AnacondaAnaconda3\python.exe" set "PY_EXE=E:\AnacondaAnaconda3\python.exe"
)
if "%PY_EXE%"=="" (
  for /d %%D in ("%LocalAppData%\Programs\Python\Python*") do (
    if exist "%%~fD\python.exe" set "PY_EXE=%%~fD\python.exe"
  )
)

if "%PY_EXE%"=="" (
  echo [ERROR] python not found.
  echo [ERROR] Tried: py -3, python, .venv\Scripts\python.exe, anaconda3/miniconda3, LocalAppData Python*
  echo [HINT] You can set TINDA_PYTHON to a full path, e.g.
  echo [HINT] set TINDA_PYTHON=E:\AnacondaAnaconda3\python.exe
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 127
)

if not exist "run_web.py" (
  echo [ERROR] run_web.py not found in: %CD%
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 2
)

set "RUN_ARGS=run_web.py --port %PORT% --port-retries %PORT_RETRIES%"
if "%RELOAD%"=="1" set "RUN_ARGS=%RUN_ARGS% --reload"

"%PY_EXE%" %PY_PRE_ARGS% %RUN_ARGS%

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [TindaAgent] exited with code %EXIT_CODE%
if "%PAUSE_ON_EXIT%"=="1" pause

endlocal & exit /b %EXIT_CODE%
