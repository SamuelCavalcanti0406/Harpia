@echo off
setlocal
chcp 65001 >nul
title HarpIA
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Iniciar-HarpIA.ps1"
if errorlevel 1 (
  echo.
  echo Nao foi possivel iniciar a HarpIA. Veja a mensagem acima.
  pause
)
endlocal
