#!/bin/bash
# Запуск support-relay на Mac: двойной клик (или в Терминале: bash start.command).
# Первый раз: ставит всё нужное и запускает мастер настройки. Потом просто запускает сервис.
cd "$(dirname "$0")" || exit 1

pause() { read -r -p "Нажми Enter, чтобы закрыть окно…" _; }

if ! command -v python3 >/dev/null 2>&1; then
  echo "Не найден Python 3. Установи его с https://www.python.org/downloads/ и запусти снова."
  pause; exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Нужен Python 3.10 или новее (сейчас: $(python3 --version 2>&1))."
  echo "Установи свежий с https://www.python.org/downloads/ и запусти снова."
  pause; exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: готовлю окружение (1–2 минуты)…"
  python3 -m venv .venv || { echo "Не получилось создать окружение."; pause; exit 1; }
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
  || { echo "Не получилось установить библиотеки. Проверь интернет и запусти снова."; pause; exit 1; }

mkdir -p data config
if [ ! -f .env ]; then
  .venv/bin/python -m app.setup_wizard || { pause; exit 1; }
  echo
fi

.venv/bin/python -m app.tools upgrade

while true; do
  echo "Сервис запущен. НЕ закрывай это окно — пока оно открыто, заявки берутся автоматически."
  echo "Остановить: Ctrl+C или закрыть окно."
  if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -i .venv/bin/python -m app.main   # Mac не уснёт, пока работает сервис
  else
    .venv/bin/python -m app.main
  fi
  status=$?
  if [ "$status" -eq 0 ] || [ "$status" -eq 130 ]; then echo "Сервис остановлен."; break; fi
  if [ "$status" -eq 3 ]; then echo "Сервис уже запущен в другом окне."; break; fi
  if [ "$status" -eq 4 ]; then echo "Перезапуск по команде /restart..."; continue; fi
  echo "Сервис упал (код $status). Перезапускаю через 15 секунд. Подробности в data/relay.log"
  sleep 15
done
pause
