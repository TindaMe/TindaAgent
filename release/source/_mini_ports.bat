@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "PORTS_FILE=%CD%\.tinda_ports.list"
for /f "usebackq tokens=1 delims= ,;" %%P in ("%PORTS_FILE%") do (
  echo P=%%P
)
