@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem Выключить автозапуск сервиса при входе в Windows.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup')+'\support-relay.lnk')"
echo Автозапуск выключен.
pause
