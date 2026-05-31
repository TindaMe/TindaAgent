@echo off
setlocal EnableExtensions

cd /d "%~dp0"

if "%~1"=="gateway" (
  call start.bat %2 %3 %4 %5 %6 %7 %8 %9
  endlocal & exit /b %ERRORLEVEL%
)

if "%~1"=="--help" (
  echo TindaAgent
  echo   tinda          CLI
  echo   tinda gateway   Web
  endlocal & exit /b 0
)

if "%~1"=="-h" (
  echo TindaAgent
  echo   tinda          CLI
  echo   tinda gateway   Web
  endlocal & exit /b 0
)

if "%~1"=="help" (
  echo TindaAgent
  echo   tinda          CLI
  echo   tinda gateway   Web
  endlocal & exit /b 0
)

call npm run tinda -- %*
endlocal & exit /b %ERRORLEVEL%
