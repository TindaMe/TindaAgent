@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "MODE=%~1"
set "ARG=%~2"
set "PORTS_FILE=%CD%\.tinda_ports.list"
set "ENV_PORTS_VAR=TINDA_ACTIVE_PORTS"
set "INTERACTIVE=0"
set "RC=0"
set "SELF_ENV=windows"

if "%MODE%"=="" (
  set "INTERACTIVE=1"
  goto :MENU
)
if /i "%MODE%"=="--help" goto :USAGE_OK
if /i "%MODE%"=="-h" goto :USAGE_OK
if /i "%MODE%"=="--list" goto :MODE_LIST
if /i "%MODE%"=="--port" goto :MODE_PORT
if /i "%MODE%"=="--all" goto :MODE_ALL
if /i "%MODE%"=="--all-env" goto :MODE_ALL_ENV

echo [ERROR] unknown arg: %MODE%
goto :USAGE

:MENU
echo.
echo [TindaAgent Stop]
echo   1. stop by port
echo   2. stop all tracked ports
echo   3. list tracked ports
echo   4. help
echo   Q. quit
choice /c 1234Q /n /m "Select: "
if errorlevel 5 goto :DONE
if errorlevel 4 goto :USAGE_OK
if errorlevel 3 goto :MODE_LIST
if errorlevel 2 (
  set "MODE=--all"
  goto :MODE_ALL
)
if errorlevel 1 (
  set /p "ARG=Input port: "
  if "%ARG%"=="" goto :DONE
  set "MODE=--port"
  goto :MODE_PORT
)
goto :DONE

:MODE_PORT
if "%ARG%"=="" (
  echo [ERROR] --port requires a number
  set "RC=2"
  goto :DONE
)
call :IS_NUMERIC "%ARG%"
if errorlevel 1 (
  echo [ERROR] invalid port: %ARG%
  set "RC=2"
  goto :DONE
)
call :STOP_BY_PORT "%ARG%"
set "RC=%ERRORLEVEL%"
goto :DONE

:MODE_LIST
call :LIST_PORTS
set "RC=0"
goto :DONE

:MODE_ALL
call :LIST_TARGET_PORTS 0
if "%PORT_LIST%"=="" if "%PORT_LIST_FOREIGN%"=="" (
  echo [stop] no tracked ports
  call :SET_ENV_PORTS ""
  break > "%PORTS_FILE%"
  set "RC=0"
  goto :DONE
)
for %%P in (%PORT_LIST%) do (
  call :STOP_BY_PORT "%%P"
)
echo [stop] all tracked ports processed
call :WRITE_PORTS_FILE "" "%PORT_LIST_FOREIGN%"
call :LIST_TARGET_PORTS 0
call :SET_ENV_PORTS "%PORT_LIST%"
set "RC=0"
goto :DONE

:MODE_ALL_ENV
call :LIST_TARGET_PORTS 1
if "%PORT_LIST%"=="" if "%PORT_LIST_FOREIGN%"=="" (
  echo [stop] no tracked ports
  call :SET_ENV_PORTS ""
  break > "%PORTS_FILE%"
  set "RC=0"
  goto :DONE
)
for %%P in (%PORT_LIST%) do (
  call :STOP_BY_PORT "%%P"
)
echo [stop] all tracked ports processed
call :SET_ENV_PORTS ""
break > "%PORTS_FILE%"
set "RC=0"
goto :DONE

:LIST_PORTS
call :LIST_TARGET_PORTS 0
if "%PORT_LIST%"=="" if "%PORT_LIST_FOREIGN%"=="" (
  echo [list] no tracked ports
  endlocal & set "RC=0" & goto :DONE
)
for %%P in (%PORT_LIST%) do (
  call :FIND_LISTEN_PIDS "%%P"
  if "!FOUND_PIDS!"=="" (
    echo [list] port %%P - stopped
  ) else (
    echo [list] port %%P - listening - pids !FOUND_PIDS!
  )
)
for %%R in (%PORT_LIST_FOREIGN%) do (
  call :SPLIT_RECORD "%%R"
  if not "!REC_PORT!"=="" echo [list] port !REC_PORT! - foreign-env:!REC_ENV! - skip-local-stop
)
endlocal & set "RC=0" & goto :DONE

:STOP_BY_PORT
setlocal EnableDelayedExpansion
set "PORT=%~1"
set "PID_FILE=%CD%\.tinda_server_%PORT%.pid"
set "KILLED=0"

if exist "!PID_FILE!" (
  set /p FOUND_PID=<"!PID_FILE!"
  call :IS_NUMERIC "!FOUND_PID!"
  if not errorlevel 1 (
    echo [stop] try pid from pid file: !FOUND_PID! ^(port !PORT!^)
    taskkill /PID !FOUND_PID! /T /F >nul 2>nul
    if not errorlevel 1 set "KILLED=1"
  )
)

call :FIND_LISTEN_PIDS "!PORT!"
if defined FOUND_PIDS (
  for %%Q in (!FOUND_PIDS!) do (
    echo [stop] kill listening pid %%Q on port !PORT!
    taskkill /PID %%Q /T /F >nul 2>nul
    if not errorlevel 1 set "KILLED=1"
  )
)

del /f /q "!PID_FILE!" >nul 2>nul
call :REMOVE_PORT "!PORT!"

if "!KILLED!"=="1" (
  echo [stop] port !PORT! processed
) else (
  echo [stop] no process found for port !PORT!
)
endlocal & exit /b 0

:FIND_LISTEN_PIDS
setlocal EnableDelayedExpansion
set "PORT=%~1"
set "RET="
for /f "tokens=5" %%Q in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  if "!RET!"=="" (
    set "RET=%%Q"
  ) else (
    set "RET=!RET! %%Q"
  )
)
endlocal & set "FOUND_PIDS=%RET%" & exit /b 0

:LIST_TARGET_PORTS
setlocal EnableDelayedExpansion
set "LIST="
set "FOREIGN="
set "INCLUDE_ENV=%~1"
if "%INCLUDE_ENV%"=="" set "INCLUDE_ENV=1"
if /i "%TINDA_PORTS_INCLUDE_ENV%"=="1" set "INCLUDE_ENV=1"
if exist "%PORTS_FILE%" (
  for /f "usebackq delims=" %%L in ("%PORTS_FILE%") do (
    set "LINE=%%L"
    set "LINE=!LINE:,= !"
    set "LINE=!LINE:;= !"
    for %%P in (!LINE!) do (
      call :SPLIT_RECORD "%%P"
      if not "!REC_PORT!"=="" (
        if /i "!REC_ENV!"=="legacy" (
          call :IS_LOCAL_LISTENING "!REC_PORT!"
          if "!LOCAL_LISTENING!"=="1" (
            call :ADD_UNIQUE LIST "!REC_PORT!"
          ) else (
            call :ADD_UNIQUE FOREIGN "!REC_ENV!:!REC_PORT!"
          )
        ) else if /i "!REC_ENV!"=="%SELF_ENV%" (
          call :ADD_UNIQUE LIST "!REC_PORT!"
        ) else (
          call :ADD_UNIQUE FOREIGN "!REC_ENV!:!REC_PORT!"
        )
      )
    )
  )
)

if "%INCLUDE_ENV%"=="1" (
  for %%P in (%TINDA_ACTIVE_PORTS%) do (
    call :SPLIT_RECORD "%%P"
    if not "!REC_PORT!"=="" (
      call :ADD_UNIQUE LIST "!REC_PORT!"
    )
  )
)

endlocal & set "PORT_LIST=%LIST%" & set "PORT_LIST_FOREIGN=%FOREIGN%" & exit /b 0

:SPLIT_RECORD
setlocal EnableDelayedExpansion
set "TOKEN=%~1"
set "REC_ENV=legacy"
set "REC_PORT=%~1"
for /f "tokens=1,2 delims=:" %%A in ("!TOKEN!") do (
  if not "%%B"=="" (
    set "REC_ENV=%%A"
    set "REC_PORT=%%B"
  )
)
set "REC_ENV=!REC_ENV:"=!"
set "REC_PORT=!REC_PORT:"=!"
if /i "!REC_ENV!"=="win" set "REC_ENV=windows"
if /i "!REC_ENV!"=="nt" set "REC_ENV=windows"
if /i "!REC_ENV!"=="gnu/linux" set "REC_ENV=linux"
call :IS_NUMERIC "!REC_PORT!"
if errorlevel 1 (
  set "REC_ENV="
  set "REC_PORT="
) else (
  if !REC_PORT! LEQ 0 (
    set "REC_ENV="
    set "REC_PORT="
  ) else if !REC_PORT! GTR 65535 (
    set "REC_ENV="
    set "REC_PORT="
  )
)
endlocal & set "REC_ENV=%REC_ENV%" & set "REC_PORT=%REC_PORT%" & exit /b 0

:IS_LOCAL_LISTENING
setlocal
set "PORT=%~1"
for /f %%Q in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  endlocal & set "LOCAL_LISTENING=1" & exit /b 0
)
endlocal & set "LOCAL_LISTENING=0" & exit /b 0

:ADD_UNIQUE
setlocal EnableDelayedExpansion
set "TARGET_VAR=%~1"
set "VAL=%~2"
call set "CUR=%%%TARGET_VAR%%%"
set "FOUND=0"
for %%X in (!CUR!) do (
  if "%%X"=="!VAL!" set "FOUND=1"
)
if "!FOUND!"=="0" (
  if "!CUR!"=="" (
    set "CUR=!VAL!"
  ) else (
    set "CUR=!CUR! !VAL!"
  )
)
endlocal & set "%~1=%CUR%" & exit /b 0

:REMOVE_PORT
setlocal EnableDelayedExpansion
set "P=%~1"
if "%P%"=="" endlocal & exit /b 0
call :LIST_TARGET_PORTS 0
set "KEEP_LOCAL="
for %%Q in (%PORT_LIST%) do (
  if not "%%Q"=="!P!" (
    if "!KEEP_LOCAL!"=="" (
      set "KEEP_LOCAL=%SELF_ENV%:%%Q"
    ) else (
      set "KEEP_LOCAL=!KEEP_LOCAL! %SELF_ENV%:%%Q"
    )
  )
)
call :WRITE_PORTS_FILE "!KEEP_LOCAL!" "%PORT_LIST_FOREIGN%"
call :LIST_TARGET_PORTS 0
call :SET_ENV_PORTS "%PORT_LIST%"
endlocal & exit /b 0

:WRITE_PORTS_FILE
setlocal
set "LOCAL=%~1"
set "FOREIGN=%~2"
break > "%PORTS_FILE%"
for %%X in (%LOCAL% %FOREIGN%) do (
  if not "%%X"=="" >> "%PORTS_FILE%" echo %%X
)
endlocal & exit /b 0

:SET_ENV_PORTS
setlocal
set "LIST=%~1"
if "%LIST%"=="" (
  set "LIST="
) else (
  set "LIST=%LIST:~0,1024%"
)
set "%ENV_PORTS_VAR%=%LIST%"
if "%LIST%"=="" (
  setx %ENV_PORTS_VAR% "\"\"" >nul 2>nul
) else (
  setx %ENV_PORTS_VAR% "%LIST%" >nul 2>nul
)
endlocal & exit /b 0

:IS_NUMERIC
setlocal
set "V=%~1"
if "%V%"=="" endlocal & exit /b 1
for /f "delims=0123456789" %%A in ("%V%") do (
  endlocal & exit /b 1
)
endlocal & exit /b 0

:USAGE_OK
echo Usage:
echo   %~nx0 --list
echo   %~nx0 --port ^<port^>
echo   %~nx0 --all
echo   %~nx0 --all-env
set "RC=0"
goto :DONE

:USAGE
echo Usage:
echo   %~nx0 --list
echo   %~nx0 --port ^<port^>
echo   %~nx0 --all
echo   %~nx0 --all-env
set "RC=2"
goto :DONE

:DONE
if "%INTERACTIVE%"=="1" (
  echo.
  pause
)
endlocal & exit /b %RC%
