@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "PORTS_FILE=%CD%\.tinda_ports.list"
for /f "usebackq tokens=1 delims= " %%P in ("%PORTS_FILE%") do (
  if not "%%P"=="" (
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
      if "!PIDS!"=="" (
        echo stopped %%P
      ) else (
        echo listening %%P !PIDS!
      )
    ) else (
      echo invalid %%P
    )
  )
)
