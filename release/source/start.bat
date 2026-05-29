@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ---- stable Windows starter (ASCII only, TypeScript runtime) ----
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
echo [TindaAgent] first-port wait: 1800ms
if "%RELOAD%"=="1" (
  echo [TindaAgent] reload: on
) else (
  echo [TindaAgent] reload: off
)
echo.

where node >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
  echo [ERROR] node not found.
  echo [HINT] Install Node.js 20+ and rerun start.bat
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 127
)

set "ENTRY=dist\web\server.bundle.js"

if not exist "%ENTRY%" (
  echo [INFO] %ENTRY% not found; building TypeScript...
  call npm run build
)

if not exist "%ENTRY%" (
  echo [ERROR] %ENTRY% not found in: %CD%
  if "%PAUSE_ON_EXIT%"=="1" pause
  exit /b 2
)

set "HOST=0.0.0.0"
set "PORT=%PORT%"
node "%ENTRY%" --host=%HOST% --port=%PORT% --port-retries=%PORT_RETRIES%

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [TindaAgent] exited with code %EXIT_CODE%
if "%PAUSE_ON_EXIT%"=="1" pause

endlocal & exit /b %EXIT_CODE%
