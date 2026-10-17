@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python\python.exe"
if not exist "%PY%" (
  echo Сначала запусти install.bat или JarvisSetup.exe
  pause
  exit /b 1
)
"%PY%" -m jarvis %*
if errorlevel 1 pause
