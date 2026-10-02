@echo off
python "%~dp0build.py" %*
if errorlevel 1 pause
