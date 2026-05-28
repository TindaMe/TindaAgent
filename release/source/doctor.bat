@echo off
setlocal EnableExtensions

cd /d "%~dp0"

where node >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
  echo [ERROR] node not found.
  exit /b 127
)

if not exist "node_modules" (
  call npm install
)

call npm run doctor -- %*
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
