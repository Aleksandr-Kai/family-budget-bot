# Запуск бота на удалённом ПК

Бот работает на Linux-машине `192.168.3.23` (пользователь `kai`, тот же компьютер,
что и для Secretary_tg_bot) как пользовательский сервис systemd.

## Что уже сделано

- На сервере создана папка `~/money_control` с `bot.py`, `report_server.py`,
  `requirements.txt` и виртуальным окружением `venv` (зависимости установлены).
- Бинарник `cloudflared` скопирован в `~/.local/bin/cloudflared` на сервере.
- Юниты скопированы и подхвачены (`systemctl --user daemon-reload`):
  `money-control-bot.service`, `money-control-report.service`,
  `money-control-tunnel.service` — все три включены и запущены
  (`enable --now`), `loginctl enable-linger kai` выполнен.

## Что нужно сделать вручную (секреты и живая база — не через ассистента)

Клод не переносит `.env` и `money_control.db` по сети — это токены и реальные
финансовые данные семьи, перенос такого вида автоматически блокируется.
Перенесите их сами:

```bash
scp /home/kai/Projects/money_control/.env kai@192.168.3.23:~/money_control/.env
scp /home/kai/Projects/money_control/money_control.db kai@192.168.3.23:~/money_control/money_control.db
ssh kai@192.168.3.23 'chmod 600 ~/money_control/.env'
```

В `.env` на сервере должны быть `BOT_TOKEN`, `GEMINI_API_KEY`, `ADMIN_CHAT_ID`
(и `EXPERIMENTAL_REPORT_URL`, если нужен — см. ниже про report_server.py).

## Остановить локальный экземпляр

Два бота с одним токеном одновременно работать не могут (Telegram отдаёт
обновления только одному — будет `409 Conflict`):

```bash
pkill -f "python.*money_control/bot.py"
```

## Включить и запустить сервис на сервере

После того как `.env` и база на месте:

```bash
ssh kai@192.168.3.23 'systemctl --user enable --now money-control-bot'
ssh kai@192.168.3.23 'loginctl enable-linger kai'
```

`enable-linger` — чтобы сервис стартовал после перезагрузки сервера без входа
пользователя в систему (уже включено для `kai`, если настраивали Secretary бота).

## Проверка

```bash
ssh kai@192.168.3.23 'systemctl --user is-active money-control-bot'
ssh kai@192.168.3.23 'journalctl --user -u money-control-bot -n 20 --no-pager'
```

Ожидается `active` и без ошибок в логе. Затем напишите боту и проверьте ответ.

## Обслуживание

Все команды — на удалённой машине (или через `ssh kai@192.168.3.23 '...'`).

| Что нужно | Команда |
|---|---|
| Смотреть лог вживую | `journalctl --user -u money-control-bot -f` |
| Перезапустить | `systemctl --user restart money-control-bot` |
| Остановить | `systemctl --user stop money-control-bot` |
| Статус | `systemctl --user status money-control-bot` |

**Обновить код.** Запустите [deploy.sh](deploy.sh) на локальной машине — он
проверяет синтаксис, копирует `bot.py`/`requirements.txt`, ставит зависимости
и перезапускает сервис. Если сервис не стартует — откатывает код на
`bot.py.prev` и пробует снова.

```bash
./deploy.sh
```

`.env` и `money_control.db` деплоем не трогаются — они только на сервере,
правьте/бэкапьте их прямо там.

**Сменить токен или ключ Gemini.** Поправьте `.env` на удалённой машине и
перезапустите сервис — `.env` читается только при старте.

**report_server.py и Cloudflare-туннель** (экспериментальный HTML-отчёт,
`/reportbeta`) тоже перенесены на сервер — отдельными сервисами:

| Сервис | Что делает |
|---|---|
| `money-control-report` | `report_server.py`, слушает `127.0.0.1:8792` |
| `money-control-tunnel` | `cloudflared tunnel --url http://127.0.0.1:8792` |

`cloudflared` — публичный бинарник Cloudflare, лежит в `~/.local/bin/cloudflared`
на сервере (без sudo). У quick-туннеля **новый случайный адрес при каждом
перезапуске** — поэтому `money-control-tunnel` не стоит перезапускать без
необходимости (`Restart=on-failure`, а не `always`). Если туннель всё же
перезапустился (или понадобилось его обновить), новый адрес нужно узнать и
прописать в `.env`, затем перезапустить бота:

```bash
ssh kai@192.168.3.23 'journalctl --user -u money-control-tunnel -n 30 --no-pager | grep -o "https://[a-zA-Z0-9.-]*trycloudflare.com"'
ssh kai@192.168.3.23 "sed -i 's#^EXPERIMENTAL_REPORT_URL=.*#EXPERIMENTAL_REPORT_URL=<новый_адрес>/#' ~/money_control/.env"
ssh kai@192.168.3.23 'systemctl --user restart money-control-bot'
```

## Что нужно знать

- Живая база (`money_control.db`) теперь только на сервере — не путайте с
  локальной копией в `/home/kai/Projects/money_control/`, она устареет.
- Токены в `.env` на сервере — права 600. Если файл попал не в те руки,
  перевыпустите токен бота в @BotFather (`/revoke`) и ключ Gemini в Google AI Studio.
