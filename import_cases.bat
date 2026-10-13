@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem База «похожих случаев»: перетащи на этот файл выгрузку Kaiten (сводный отчёт, xlsx).
rem Или положи xlsx в эту папку и запусти двойным кликом — возьму самый свежий.
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запусти start.bat.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m app.tools cases %1
pause
