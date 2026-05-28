@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=--show"

if /i "%MODE%"=="--help" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0status.ps1" -Mode --help
  set "RC=%ERRORLEVEL%"
  endlocal & exit /b %RC%
)
if /i "%MODE%"=="-h" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0status.ps1" -Mode --help
  set "RC=%ERRORLEVEL%"
  endlocal & exit /b %RC%
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0status.ps1" -Mode "%MODE%"
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
