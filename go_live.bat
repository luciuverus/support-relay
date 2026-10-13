@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem Выключить тестовый режим: сервис начнёт по-настоящему брать заявки и писать людям.
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запусти start.bat.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m app.tools live
pause
