@echo off
:: TindaAgent 启动脚本 (Windows)
set PORT=%1
if "%PORT%"=="" set PORT=8000

echo  TindaAgent 启动中...
echo    地址: http://127.0.0.1:%PORT%
echo    按 Ctrl+C 停止
echo.

start "" "http://127.0.0.1:%PORT%"
python run_web.py --port %PORT%
