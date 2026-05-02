@echo off
setlocal EnableExtensions

cd /d "%~dp0"

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

if "%PY_EXE%"=="" (
  if not "%TINDA_PYTHON%"=="" (
    if exist "%TINDA_PYTHON%" set "PY_EXE=%TINDA_PYTHON%"
  )
)

if "%PY_EXE%"=="" (
  if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
)
if "%PY_EXE%"=="" (
  if exist "%~dp0venv\Scripts\python.exe" set "PY_EXE=%~dp0venv\Scripts\python.exe"
)
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
  echo [ERROR] python not found.
  exit /b 127
)

if not exist "doctor.py" (
  echo [ERROR] doctor.py not found in: %CD%
  exit /b 2
)

"%PY_EXE%" %PY_PRE_ARGS% doctor.py %*
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
