@echo off
setlocal EnableExtensions
set "BIN=%USERPROFILE%\.local\bin"
if not exist "%BIN%" mkdir "%BIN%"

set "OUT=%BIN%\tinda.bat"
set "TARGET=%~dp0tinda.bat"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$bin = [IO.Path]::GetFullPath($env:BIN); $target = [IO.Path]::GetFullPath($env:TARGET); if ([IO.Path]::GetPathRoot($bin) -ne [IO.Path]::GetPathRoot($target)) { exit 10 }; $rel = [IO.Path]::GetRelativePath($bin, $target); Set-Content -LiteralPath $env:OUT -Encoding ASCII -Value ('@echo off' + [Environment]::NewLine + 'call "%%~dp0' + $rel + '" %%*')"

if errorlevel 10 (
  echo [ERROR] Cannot install a relative PATH launcher across different drives.
  echo [HINT] Run tinda.bat from this project directory, or add this directory to PATH yourself.
  endlocal & exit /b 10
)

if errorlevel 1 endlocal & exit /b %ERRORLEVEL%

echo done - %OUT%
echo tinda --help
pause
endlocal
