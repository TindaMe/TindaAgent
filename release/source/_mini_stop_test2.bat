@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "MODE=%~1"
if /i "%MODE%"=="--list" goto :MODE_LIST
echo no
exit /b 0
:MODE_LIST
call :LIST_PORTS
echo done
exit /b 0
:LIST_PORTS
setlocal EnableDelayedExpansion
echo list file: %CD%
endlocal & exit /b 0
