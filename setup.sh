#!/usr/bin/env bash
# Устанавливает бота на этой машине "с нуля": venv, зависимости, .env.
# Ничего не удаляет и не трогает, если .env уже существует — просто
# донастроит venv/зависимости.
# Использование: ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== Установка family-budget-bot =="
echo

if [ ! -d .venv ]; then
	echo "==> Создаю виртуальное окружение (.venv)"
	python3 -m venv .venv
fi

echo "==> Устанавливаю зависимости"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if [ -f .env ]; then
	echo
	echo "==> .env уже существует, не трогаю его."
else
	echo
	echo "Нужны два значения. Если нет под рукой — Enter, впишете позже в .env."
	echo

	read -rp "Токен бота (от @BotFather в Telegram): " bot_token
	read -rp "Ключ Gemini (https://aistudio.google.com/app/apikey): " gemini_key

	cp .env.example .env
	[ -n "$bot_token" ] && sed -i "s#^BOT_TOKEN=.*#BOT_TOKEN=$bot_token#" .env
	[ -n "$gemini_key" ] && sed -i "s#^GEMINI_API_KEY=.*#GEMINI_API_KEY=$gemini_key#" .env

	echo
	echo "==> .env создан. ADMIN_CHAT_ID оставлен пустым — так и должно быть:"
	echo "    вы станете админом автоматически, когда напишете боту /start."
fi

echo
echo "== Готово =="
echo
echo "Дозаполните .env, если что-то пропустили, затем запустите бота:"
echo
echo "    .venv/bin/python3 bot.py"
echo
echo "Откройте бота в Telegram и отправьте /start — вы станете администратором."
echo "Дальше /invite пришлёт ссылку, чтобы позвать остальных."
echo
echo "Чтобы бот работал постоянно (не только пока открыт терминал) — см. раздел"
echo "\"Автозапуск\" в INSTALL.md."
