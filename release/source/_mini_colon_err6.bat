@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "MODE=%~1"
if /i "%MODE%"=="--list" goto :MODE_LIST
echo other
goto :DONE
:MODE_LIST
echo [list] tracked ports file: %CD%\.tinda_ports.list
set "HAS_ANY=0"
for /f "usebackq tokens=1 delims= " %%P in ("%CD%\.tinda_ports.list") do (
  if not "%%P"=="" (
    set "HAS_ANY=1"
    echo(%%P|findstr /r "^[0-9][0-9]*$" >nul
    if not errorlevel 1 (
      set "PIDS="
      for /f "tokens=5" %%Q in ('netstat -ano ^| findstr /r /c:":%%P .*LISTENING"') do (
        if "!PIDS!"=="" (
          set "PIDS=%%Q"
        ) else (
          set "PIDS=!PIDS!,%%Q"
        )
      )
      echo [list] port %%P pids=!PIDS!
    ) else (
      echo [list] port %%P - invalid
    )
  )
)
if "!HAS_ANY!"=="0" echo [list] no tracked ports
goto :DONE
:DONE
exit /b 0
