@echo off
set "BIN=%USERPROFILE%\.local\bin"
if not exist "%BIN%" mkdir "%BIN%"

set "OUT=%BIN%\tinda.bat"
> "%OUT%" echo @echo off
>>"%OUT%" echo cd /d "%~dp0"
>>"%OUT%" echo if "%%1"=="gateway" call start.bat %%2 %%3 %%4 %%5 %%6 ^& goto :eof
>>"%OUT%" echo if "%%1"=="--help" echo TindaAgent ^& echo   tinda          CLI ^& echo   tinda gateway   Web ^& goto :eof
>>"%OUT%" echo npm run tinda -- %%*

echo done - %OUT%
echo tinda --help
pause
