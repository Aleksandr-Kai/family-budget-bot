#!/usr/bin/env bash
# Обновляет бота на удалённой машине: копирует код и перезапускает сервис.
# .env и money_control.db НЕ копируются деплоем — они уже на сервере и
# содержат токены/финансовые данные; живая база на удалённой машине всегда
# главнее локальной.
# Использование: ./deploy.sh
# Переменные: DEPLOY_HOST (по умолчанию kai@192.168.3.23), DEPLOY_DIR (по умолчанию money_control).
set -euo pipefail

HOST="${DEPLOY_HOST:-kai@192.168.3.23}"
DIR="${DEPLOY_DIR:-money_control}"
SERVICE="money-control-bot"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST")

cd "$(dirname "$0")"

echo "==> Проверка синтаксиса"
python3 -m py_compile bot.py

echo "==> Проверка доступа к $HOST"
"${SSH[@]}" 'echo ok' >/dev/null || { echo "Нет доступа по ssh-ключу. Выполните: ssh-copy-id $HOST" >&2; exit 1; }

echo "==> Копирование кода (бот сохраняется как bot.py.prev перед заменой)"
"${SSH[@]}" "cd $DIR && [ -f bot.py ] && cp bot.py bot.py.prev || true"
scp -q -o BatchMode=yes bot.py requirements.txt "$HOST:$DIR/"

echo "==> Установка зависимостей (если requirements.txt менялся) и перезапуск"
"${SSH[@]}" "cd $DIR && venv/bin/pip install --quiet -r requirements.txt && systemctl --user restart $SERVICE"

sleep 4
if "${SSH[@]}" "systemctl --user is-active --quiet $SERVICE"; then
	echo "==> Готово: сервис $SERVICE работает"
	"${SSH[@]}" "journalctl --user -u $SERVICE -n 5 --no-pager" | cut -c1-160
else
	echo "!! Сервис не запустился, откатываю код на прежнюю версию" >&2
	"${SSH[@]}" "journalctl --user -u $SERVICE -n 15 --no-pager" | cut -c1-160 >&2
	"${SSH[@]}" "cd $DIR && [ -f bot.py.prev ] && cp bot.py.prev bot.py && systemctl --user restart $SERVICE" || true
	exit 1
fi
