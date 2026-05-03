@echo off
setlocal EnableDelayedExpansion
set "PORTS_FILE=%CD%\.tinda_ports.list"
for /f "usebackq delims=" %%P in ("%PORTS_FILE%") do (
  for /f "tokens=1 delims= " %%T in ("%%P") do (
    if not "%%T"=="" (
      echo P=%%T
    )
  )
)
