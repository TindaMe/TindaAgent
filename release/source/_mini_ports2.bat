@echo off
for /f "tokens=1 delims=,;" %%P in ("8000,8010;8020") do echo P=%%P
