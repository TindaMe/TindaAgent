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
for /f "tokens=1 delims= " %%T in ("8000 ") do (
  if not "%%T"=="" (
    call :LIST_ONE "%%T"
  )
)
exit /b 0
:LIST_ONE
echo one %1
exit /b 0
