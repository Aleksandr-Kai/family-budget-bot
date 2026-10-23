"""Backend мини-приложения (Telegram Mini App) для money_control.

Дополняет обычного чат-бота (bot.py) — не заменяет ввод трат текстом/голосом/
фото, а даёт отдельный экран со сводками, бюджетами и списком покупок.
Переиспользует все функции работы с базой из bot.py напрямую.

Каждый запрос должен нести initData от Telegram.WebApp в заголовке
X-Telegram-Init-Data — подпись проверяется по алгоритму Telegram
(см. verify_init_data). Без валидной подписи доступа нет.

Для локальной отладки без реального Telegram (когда initData взять неоткуда)
можно задать WEBAPP_DEV_CHAT_ID в .env — тогда проверка подписи ПОЛНОСТЬЮ
отключается и все запросы считаются от этого chat_id. Использовать только
на своей машине, никогда в проде.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from functools import wraps
from urllib.parse import parse_qsl

from flask import Flask, g, jsonify, request, send_from_directory

import bot

logger = logging.getLogger("webapp_server")
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEV_CHAT_ID = os.getenv("WEBAPP_DEV_CHAT_ID")

app = Flask(__name__, static_folder=None)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


def verify_init_data(init_data: str, bot_token: str):
    """Проверяет подпись Telegram.WebApp.initData. Возвращает распарсенные
    данные (включая user) при успехе, иначе None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    user = None
    if "user" in data:
        try:
            user = json.loads(data["user"])
        except (json.JSONDecodeError, TypeError):
            user = None
    return {"user": user, "raw": data}


def require_auth(f):
    """Декоратор: проверяет initData, кладёт chat_id/display_name в g,
    проверяет доступ (approved/admin) — как ensure_access в самом боте."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if DEV_CHAT_ID:
            logger.warning("WEBAPP_DEV_CHAT_ID активен — проверка подписи initData отключена!")
            chat_id = int(DEV_CHAT_ID)
            display = "Dev"
        else:
            init_data = request.headers.get("X-Telegram-Init-Data", "")
            result = verify_init_data(init_data, BOT_TOKEN)
            if not result or not result.get("user"):
                return jsonify({"error": "unauthorized"}), 401
            chat_id = result["user"]["id"]
            display = (
                result["user"].get("first_name")
                or result["user"].get("username")
                or "Без имени"
            )

        if not bot.is_admin(chat_id):
            status = bot.get_access_status(chat_id)
            if status != "approved":
                return jsonify({"error": "forbidden", "status": status or "none"}), 403

        g.chat_id = chat_id
        g.display_name = display
        return f(*args, **kwargs)

    return wrapper


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.get("/api/me")
@require_auth
def api_me():
    return jsonify(
        {
            "chat_id": g.chat_id,
            "display_name": g.display_name,
            "is_admin": bot.is_admin(g.chat_id),
        }
    )


def _category_totals(rows):
    totals = {}
    for r in rows:
        category = r["category"] or "Другое"
        totals[category] = totals.get(category, 0.0) + r["amount"]
    return sorted(
        ({"category": c, "amount": a} for c, a in totals.items()),
        key=lambda x: -x["amount"],
    )


@app.get("/api/dashboard")
@require_auth
def api_dashboard():
    period_start = bot.current_period_start()
    rows = bot.get_expenses(period_start)
    shared_total = bot.get_shared_budget_total()
    shared_spent = bot.get_spent_since(period_start)

    personal = bot.get_personal_budget(g.chat_id)
    personal_spent = bot.get_spent_since_by_chat(g.chat_id, period_start)

    today_start = bot.datetime.now().strftime("%Y-%m-%dT00:00:00")
    today_total = sum(r["amount"] for r in rows if r["created_at"] >= today_start)

    return jsonify(
        {
            "period_start": period_start[:10],
            "shared_budget_total": shared_total,
            "shared_spent": shared_spent,
            "shared_remaining": shared_total - shared_spent,
            "personal_budget": personal["amount"] if personal else None,
            "personal_spent": personal_spent,
            "personal_remaining": (personal["amount"] - personal_spent) if personal else None,
            "today_total": today_total,
        }
    )


@app.get("/api/expenses")
@require_auth
def api_expenses():
    scope = request.args.get("scope", "period")
    since = bot.datetime.now().strftime("%Y-%m-%dT00:00:00") if scope == "today" else bot.current_period_start()
    rows = bot.get_expenses(since)
    items = [
        {
            "created_at": r["created_at"],
            "amount": r["amount"],
            "description": r["description"],
            "category": r["category"] or "Другое",
            "added_by": r["added_by"],
        }
        for r in reversed(rows)
    ]
    return jsonify({"items": items, "total": sum(i["amount"] for i in items)})


@app.get("/api/categories")
@require_auth
def api_categories():
    rows = bot.get_expenses(bot.current_period_start())
    totals = _category_totals(rows)
    return jsonify({"totals": totals, "total": sum(t["amount"] for t in totals)})


@app.get("/api/months")
@require_auth
def api_months():
    rows = bot.get_expenses()
    by_month = {}
    order = []
    for r in rows:
        dt = bot.datetime.fromisoformat(r["created_at"])
        key = f"{dt.year}-{dt.month:02d}"
        if key not in by_month:
            by_month[key] = []
            order.append(key)
        by_month[key].append(r)
    months = [
        {
            "key": key,
            "label": f"{bot.MONTHS_NOM[int(key[5:7]) - 1]} {key[:4]}",
            "totals": _category_totals(by_month[key]),
            "total": sum(r["amount"] for r in by_month[key]),
        }
        for key in order
    ]
    return jsonify({"months": months})


@app.get("/api/budgets")
@require_auth
def api_budgets():
    rows = bot.get_all_personal_budgets()
    return jsonify(
        {"budgets": [{"display_name": r["display_name"], "amount": r["amount"]} for r in rows]}
    )


@app.post("/api/budget")
@require_auth
def api_set_budget():
    body = request.get_json(silent=True) or {}
    raw = str(body.get("amount", "")).strip()
    if not raw:
        return jsonify({"error": "amount required"}), 400
    amount, error = bot.resolve_budget_amount(g.chat_id, raw)
    if error:
        return jsonify({"error": error}), 400
    if amount is None:
        return jsonify({"error": "Не удалось распознать сумму"}), 400
    bot.set_personal_budget(g.chat_id, amount, g.display_name)
    return jsonify({"amount": amount})


@app.get("/api/shopping")
@require_auth
def api_shopping_list():
    rows = bot.get_shopping_list()
    return jsonify({"items": [{"id": r["id"], "item": r["item"], "done": bool(r["done"])} for r in rows]})


@app.post("/api/shopping/add")
@require_auth
def api_shopping_add():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()
    items = bot.split_items(text)
    if not items:
        return jsonify({"error": "Не нашёл позиций"}), 400
    items = asyncio.run(bot.normalize_items_to_nominative(items))
    bot.add_shopping_items(items, g.display_name)
    asyncio.run(
        bot.broadcast_message(
            f"🛒 {g.display_name} добавил(а) в список покупок:\n"
            + "\n".join(f"• {i}" for i in items),
            reply_markup=bot.shopping_keyboard(),
        )
    )
    return jsonify({"added": items})


@app.post("/api/shopping/done")
@require_auth
def api_shopping_done():
    body = request.get_json(silent=True) or {}
    names = body.get("names") or []
    matched = asyncio.run(bot.mark_shopping_items_done_by_names(names))
    return jsonify({"matched": matched})


@app.post("/api/shopping/delete")
@require_auth
def api_shopping_delete():
    body = request.get_json(silent=True) or {}
    names = body.get("names") or []
    deleted = asyncio.run(bot.delete_shopping_items_by_names(names))
    return jsonify({"deleted": deleted})


@app.post("/api/shopping/delete_done")
@require_auth
def api_shopping_delete_done():
    deleted = bot.delete_done_shopping_items()
    return jsonify({"deleted": deleted})


if __name__ == "__main__":
    bot.init_db()
    app.run(host="127.0.0.1", port=8793, debug=bool(DEV_CHAT_ID))
