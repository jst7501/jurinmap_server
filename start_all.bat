@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
pushd "%~dp0"
python start_all.py
popd
pause
