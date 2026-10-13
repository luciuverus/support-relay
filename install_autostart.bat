@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem Автозапуск: сервис будет стартовать сам, когда ты входишь в Windows, окно - свёрнутым.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\support-relay.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
if errorlevel 1 (
  echo Не получилось включить автозапуск.
) else (
  echo Автозапуск включён: сервис будет стартовать сам при входе в Windows.
  echo Выключить: remove_autostart.bat
)
pause
