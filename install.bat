@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Джарвис — установка

echo ============================================================
echo   Джарвис — офлайн голосовой ассистент. Установка
echo ============================================================
echo.

rem ---------- 1. Python 3.10-3.12 ----------
set "PY="
for %%V in (3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [!] Не найден Python 3.10-3.12. Установи его с https://www.python.org/downloads/
  echo     и отметь галочку "Add python.exe to PATH". Затем запусти install.bat снова.
  pause
  exit /b 1
)
for /f "delims=" %%i in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%i
echo [OK] Python %PYVER% (%PY%)

rem ---------- 2. Свободное место ----------
for /f "delims=" %%i in ('powershell -NoProfile -Command "[math]::Round((Get-PSDrive -Name ('%~d0'.Substring(0,1))).Free/1GB,1)"') do set FREEGB=%%i
echo [i] Свободно на диске %~d0 : %FREEGB% ГБ
echo     Нужно примерно: Python-пакеты ~2 ГБ, Whisper small ~0.5 ГБ, голос Piper ~0.06 ГБ.
echo     Модели Ollama (хранятся отдельно, обычно на C:): qwen2.5:7b 4.7 ГБ, qwen2.5-coder:7b 4.7 ГБ,
echo     qwen2.5vl:3b 3.2 ГБ, nomic-embed-text 0.3 ГБ.
echo.

rem ---------- 3. venv + зависимости ----------
if not exist ".venv\Scripts\python.exe" (
  echo [..] Создаю виртуальное окружение .venv
  %PY% -m venv .venv || (echo [!] Не удалось создать venv & pause & exit /b 1)
)
set "VPY=%~dp0.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip wheel >nul
echo [..] Ставлю зависимости (несколько минут)...
"%VPY%" -m pip install -r requirements.txt || (echo [!] Ошибка установки пакетов & pause & exit /b 1)
echo [OK] Пакеты установлены

rem ---------- 4. Голос Piper + Whisper ----------
echo [..] Скачиваю голос Piper и модель распознавания Whisper
"%VPY%" scripts\download_models.py || echo [!] Часть моделей не скачалась - повтори install.bat позже

rem ---------- 5. Ollama ----------
set "OLLAMA="
where ollama >nul 2>&1 && set "OLLAMA=ollama"
if not defined OLLAMA if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not defined OLLAMA (
  echo.
  echo ------------------------------------------------------------
  echo  Ollama не установлена. Это локальный "мозг" Джарвиса.
  echo  1. Скачай установщик: https://ollama.com/download  (Download for Windows)
  echo  2. Запусти OllamaSetup.exe и пройди установку
  echo  3. Открой НОВОЕ окно терминала и проверь: ollama --version
  echo  4. Запусти install.bat ещё раз - модели скачаются сами
  echo ------------------------------------------------------------
  where winget >nul 2>&1 && (
    choice /C YN /M "Установить Ollama автоматически через winget сейчас"
    if !errorlevel! EQU 1 (
      winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
      if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
    )
  )
)

if defined OLLAMA (
  rem необязательно: хранить модели Ollama на D:
  if exist D:\ if not defined OLLAMA_MODELS (
    choice /C YN /M "Хранить модели Ollama на D:\JarvisData\ollama (экономит место на C:)"
    if !errorlevel! EQU 1 (
      mkdir "D:\JarvisData\ollama" 2>nul
      setx OLLAMA_MODELS "D:\JarvisData\ollama" >nul
      set "OLLAMA_MODELS=D:\JarvisData\ollama"
      echo [i] OLLAMA_MODELS=D:\JarvisData\ollama. Перезапусти Ollama ^(значок в трее -^> Quit, затем запусти снова^).
      taskkill /IM "ollama app.exe" /F >nul 2>&1
      taskkill /IM ollama.exe /F >nul 2>&1
      start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" 2>nul
      timeout /t 5 >nul
    )
  )
  for /f "delims=" %%m in ('"%VPY%" -c "from jarvis.config import load_config;print(load_config()['llm']['model'])"') do set MODEL=%%m
  echo [..] Скачиваю основную модель !MODEL! ^(~4.7 ГБ^)
  "!OLLAMA!" pull !MODEL!
  echo [..] Модель эмбеддингов для памяти nomic-embed-text ^(~0.3 ГБ^)
  "!OLLAMA!" pull nomic-embed-text
  choice /C YN /M "Скачать модель для кода и 3D qwen2.5-coder:7b (~4.7 ГБ)"
  if !errorlevel! EQU 1 "!OLLAMA!" pull qwen2.5-coder:7b
  choice /C YN /M "Скачать модель зрения для экрана qwen2.5vl:3b (~3.2 ГБ)"
  if !errorlevel! EQU 1 "!OLLAMA!" pull qwen2.5vl:3b
)

rem ---------- 6. Автозагрузка (включена по умолчанию) ----------
"%VPY%" -c "from jarvis.config import load_config, PROJECT_ROOT; from jarvis.platform_utils import set_autostart; c=load_config(); print('[OK] Автозагрузка включена' if c['autostart'].get('enabled', True) and set_autostart(True, PROJECT_ROOT) else '[i] Автозагрузка выключена')"

echo.
echo ============================================================
echo  Готово! Запуск: run.bat  (окно с голограммой + значок в трее)
echo  Текстовый режим: run_text.bat   Проверка: run.bat --check
echo ============================================================
pause
