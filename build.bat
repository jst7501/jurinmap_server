@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
pushd "%~dp0dashboard"
set NODE_OPTIONS=--max-old-space-size=4096
npm run build
popd
