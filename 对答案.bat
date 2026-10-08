@echo off
title AI Trainer Level-3 Answer Checker
cd /d "%~dp0"
set "PY=E:\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "对答案系统\对答案引擎.py"

pause
