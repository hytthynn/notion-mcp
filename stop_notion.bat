@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
color 0F
title MCP-мост для Notion — Остановка
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" -Action stop
set "result=%errorlevel%"
if /I not "%MCP_NO_PAUSE%"=="1" (
  echo    Нажмите любую клавишу, чтобы закрыть окно…
  pause >nul
)
exit /b %result%
