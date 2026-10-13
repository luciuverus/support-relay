#!/bin/bash
# Mac: выключить тестовый режим — сервис начнёт по-настоящему брать заявки и писать людям.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then echo "Сначала запусти start.command."; read -r -p "Enter…" _; exit 1; fi
.venv/bin/python -m app.tools live
read -r -p "Нажми Enter, чтобы закрыть окно…" _
