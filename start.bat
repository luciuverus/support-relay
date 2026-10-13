@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title support-relay

rem Запуск support-relay на Windows: двойной клик по этому файлу.
rem Первый раз ставит всё нужное и запускает мастер настройки, потом просто запускает сервис.
rem Если сервис упал, через 15 секунд он перезапустится сам.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Не найден Python. Установи его с https://www.python.org/downloads/
  echo При установке ОБЯЗАТЕЛЬНО отметь галочку "Add python.exe to PATH", потом запусти этот файл снова.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Нужен Python 3.10 или новее. Установи свежий с https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: готовлю окружение, 1-2 минуты...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Не получилось создать окружение.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  if exist ".venv\installed.ok" (
    echo Не получилось обновить библиотеки - нет интернета? Запускаю с уже установленными.
  ) else (
    echo Не получилось установить библиотеки. Проверь интернет и запусти снова.
    pause
    exit /b 1
  )
) else (
  echo ok> ".venv\installed.ok"
)

if not exist data mkdir data
if not exist config mkdir config
if not exist ".env" (
  ".venv\Scripts\python.exe" -m app.setup_wizard
  if errorlevel 1 (
    pause
    exit /b 1
  )
  echo.
)
".venv\Scripts\python.exe" -m app.tools upgrade

:run
echo Сервис запущен. НЕ закрывай это окно: пока оно открыто, заявки берутся автоматически.
echo Окно можно свернуть. Остановить: Ctrl+C или закрыть окно.
".venv\Scripts\python.exe" -m app.main
set "CODE=%errorlevel%"
if "%CODE%"=="0" goto stopped
if "%CODE%"=="3" goto twice
if "%CODE%"=="4" goto restart
echo Сервис упал - код %CODE%. Перезапускаю через 15 секунд. Подробности в data\relay.log
timeout /t 15 /nobreak >nul
goto run

:twice
echo Сервис уже запущен в другом окне. Это окно можно закрыть.
pause
exit /b 0

:stopped
echo Сервис остановлен.
pause
exit /b 0

:restart
echo Перезапуск по команде /restart...
goto run
