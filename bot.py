import calendar
import html
import json
import logging
import os
import re
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "money_control.db")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3.5-flash-lite"
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID")) if os.getenv("ADMIN_CHAT_ID") else None

# Временный адрес туннеля для экспериментального HTML-отчёта (Mini App).
# Меняется при перезапуске cloudflared — тогда нужно обновить .env.
EXPERIMENTAL_REPORT_URL = os.getenv("EXPERIMENTAL_REPORT_URL")

CANCEL_TEXT = "Отмена"
CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[CANCEL_TEXT]], resize_keyboard=True, one_time_keyboard=True
)

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# "2х500 связь" / "2x500 связь" / "2*20 вода" -> количество x цена за штуку, описание
MULT_RE = re.compile(r"^(\d+)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)\s+(.+)$", re.IGNORECASE)
# "40 вода" -> сумма, описание
SIMPLE_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s+(.+)$")
# описание вида "... по 55" или заканчивающееся числом — похоже на "количество
# по цене за штуку" в свободной форме ("3 кг огурцов по 55"), а не на простую
# "сумма + описание". В таких случаях лучше не угадывать, а отдать Gemini.
AMBIGUOUS_RE = re.compile(r"\bпо\s+\d+(?:[.,]\d+)?\b|\d+(?:[.,]\d+)?\s*$", re.IGNORECASE)


def parse_expense_line(line: str):
    line = line.strip()
    if not line:
        return None

    m = MULT_RE.match(line)
    if m:
        qty = int(m.group(1))
        unit_price = float(m.group(2).replace(",", "."))
        description = m.group(3).strip()
        amount = qty * unit_price
        display_desc = f"{description} ({qty}×{unit_price:g})"
        return amount, display_desc

    m = SIMPLE_RE.match(line)
    if m:
        description = m.group(2).strip()
        if AMBIGUOUS_RE.search(description):
            return None
        amount = float(m.group(1).replace(",", "."))
        return amount, description

    return None


EXPENSE_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "expenses": genai_types.Schema(
            type=genai_types.Type.ARRAY,
            items=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "amount": genai_types.Schema(
                        type=genai_types.Type.NUMBER,
                        description="Сумма траты в сомах",
                    ),
                    "description": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        description="Краткое описание траты (1-4 слова)",
                    ),
                },
                required=["amount", "description"],
            ),
        )
    },
    required=["expenses"],
)

GEMINI_SYSTEM_INSTRUCTION = (
    "Ты распознаёшь траты в свободном тексте на русском языке для бота учёта "
    "расходов. Извлеки все траты (сумма в сомах и краткое описание, 1-4 слова) "
    "из сообщения пользователя. Если явных сумм денег в тексте нет, верни "
    "пустой список expenses."
)


def _extract_expenses_from_json(raw_text: str):
    data = json.loads(raw_text)
    result = []
    for e in data.get("expenses", []):
        try:
            amount = float(e["amount"])
            description = str(e["description"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if amount > 0 and description:
            result.append((amount, description))
    return result


async def parse_with_gemini(text: str):
    """Пытается распознать траты в сложном свободном тексте через Gemini.

    Возвращает список (amount, description) или None, если вызов не удался
    (нет ключа, ошибка API и т.п.) — тогда вызывающий код должен вести себя
    так, будто интеграции с Gemini нет.
    """
    if not gemini_client:
        return None
    try:
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=text,
            config=genai_types.GenerateContentConfig(
                system_instruction=GEMINI_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=EXPENSE_SCHEMA,
            ),
        )
        return _extract_expenses_from_json(response.text)
    except Exception:
        logger.exception("Ошибка обращения к Gemini API")
        return None


GEMINI_IMAGE_SYSTEM_INSTRUCTION = (
    "Тебе присылают фотографию или скриншот, полученные ботом учёта расходов. "
    "Это НЕ обязательно чек — это может быть скриншот перевода или платежа, "
    "банковское уведомление, страница интернет-магазина с заказом, а может "
    "быть вообще не связано с тратами (случайное фото, документ, мем, "
    "скриншот переписки и т.п.) — не считай заранее, что на изображении есть "
    "покупка. Извлеки траты (сумма в сомах и краткое описание, 1-4 слова) "
    "только если на изображении ЯВНО видна конкретная сумма и что она "
    "означает. Если это чек с несколькими позициями — верни каждую позицию "
    "отдельной тратой. Если явных трат нет или изображение не про расходы — "
    "верни пустой список expenses, ничего не придумывай."
)


async def parse_image_with_gemini(image_bytes: bytes, mime_type: str = "image/jpeg"):
    """Пытается распознать траты на изображении (чек, скриншот платежа и т.п.)
    через Gemini. Возвращает список (amount, description), пустой список,
    если трат не найдено, или None при ошибке вызова."""
    if not gemini_client:
        return None
    try:
        image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[image_part],
            config=genai_types.GenerateContentConfig(
                system_instruction=GEMINI_IMAGE_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=EXPENSE_SCHEMA,
            ),
        )
        return _extract_expenses_from_json(response.text)
    except Exception:
        logger.exception("Ошибка обращения к Gemini API (изображение)")
        return None


GEMINI_TRANSCRIBE_INSTRUCTION = (
    "Расшифруй голосовое сообщение дословно на русском языке (если сказано на "
    "другом языке — расшифруй на нём же). Верни только текст расшифровки, без "
    "комментариев, кавычек и пояснений."
)


async def transcribe_voice_with_gemini(audio_bytes: bytes, mime_type: str = "audio/ogg"):
    """Расшифровывает голосовое сообщение в текст через Gemini. Возвращает
    строку с текстом, пустую строку, если ничего не расслышано, или None при
    ошибке вызова."""
    if not gemini_client:
        return None
    try:
        audio_part = genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[audio_part],
            config=genai_types.GenerateContentConfig(
                system_instruction=GEMINI_TRANSCRIBE_INSTRUCTION,
            ),
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("Ошибка обращения к Gemini API (расшифровка голосового)")
        return None


CATEGORIES = ["Продукты", "Кафе", "Транспорт", "Дом", "Здоровье", "Личное", "Собака", "Другое"]

# Порядок важен: более специфичные категории проверяются раньше общей
# "Продукты", иначе случайное совпадение подстроки (например, "рис" внутри
# "Рисовый сендвич") перетягивает результат не туда.
CATEGORY_KEYWORDS = {
    "Кафе": [
        "кофе", "латте", "кафе", "столовая", "ресторан", "сендвич",
        "пирожн", "тарталетк", "капучино", "обед", "ужин", "завтрак",
        "щербет",
    ],
    "Транспорт": [
        "такси", "тулпар", "яндекс", "проезд", "автобус", "метро",
        "бензин", "заправк",
    ],
    "Дом": [
        "домофон", "мышеловк", "файл-карман", "канцеляр", "ремонт",
        "сантех", "лампочк", "батарейк",
    ],
    "Здоровье": [
        "стрепсилс", "аптек", "лекарств", "витамин", "таблетк", "врач",
        "больниц",
    ],
    "Личное": [
        "стрижк", "маникюр", "экран", "следочк", "одежд", "обувь", "космет",
    ],
    "Собака": [
        "требух", "собак", "корм для собак", "поводок", "ошейник",
    ],
    "Продукты": [
        "хлеб", "вода", "молоко", "яйц", "сыр", "колбас", "огурц", "помидор",
        "лук", "перец", "лепёшк", "лепешк", "фасол", "тыкв",
        "мандарин", "лимон", "банан", "крыль", "ножк", "грудк", "кабачк",
        "морковк", "форель", "овощ", "водка", "пиво", "вино", "печень",
        "пакет", "конфет", "круп", "мясо", "рис", "сахар", "соль", "масло",
        "яблок", "виноград", "творог", "сметан", "йогурт",
        "жвачк", "картошк", "картофел",
    ],
}


def guess_category_locally(description: str):
    text = description.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None


CATEGORY_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "category": genai_types.Schema(type=genai_types.Type.STRING, enum=CATEGORIES),
    },
    required=["category"],
)

CATEGORY_SYSTEM_INSTRUCTION = (
    "Определи наиболее подходящую категорию расхода по его краткому описанию. "
    f"Выбери ровно одну категорию из списка: {', '.join(CATEGORIES)}. "
    "Если ничего явно не подходит, выбери 'Другое'."
)


async def classify_category_with_gemini(description: str) -> str:
    if not gemini_client:
        return "Другое"
    try:
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=description,
            config=genai_types.GenerateContentConfig(
                system_instruction=CATEGORY_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=CATEGORY_SCHEMA,
            ),
        )
        data = json.loads(response.text)
        category = data.get("category")
        return category if category in CATEGORIES else "Другое"
    except Exception:
        logger.exception("Ошибка обращения к Gemini API (категория)")
        return "Другое"


async def determine_category(description: str) -> str:
    """Определяет категорию расхода: сначала локально по ключевым словам,
    затем по кэшу уже спрошенных у Gemini описаний, и только если ничего не
    подошло — обращается к Gemini (и сохраняет ответ в кэш)."""
    local = guess_category_locally(description)
    if local:
        return local

    cached = get_cached_category(description)
    if cached:
        return cached

    category = await classify_category_with_gemini(description)
    cache_category(description, category)
    return category


SHOPPING_MATCH_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "matched_numbers": genai_types.Schema(
            type=genai_types.Type.ARRAY,
            items=genai_types.Schema(type=genai_types.Type.INTEGER),
        ),
    },
    required=["matched_numbers"],
)

SHOPPING_MATCH_INSTRUCTION = (
    "Тебе дан пронумерованный список запланированных покупок и текст (описание "
    "траты или сообщение о выполнении). Определи, какие пункты списка "
    "соответствуют тому же товару, что назван в тексте — учитывай падежи, "
    "синонимы, опечатки и расхождения при расшифровке голоса (одно и то же "
    "может быть написано по-разному, например 'билевую верёвку' и 'бельевая "
    "верёвка' — это один и тот же товар). Верни номера подходящих пунктов "
    "(matched_numbers). Если совпадений нет — верни пустой список."
)


async def match_shopping_items_with_gemini(text: str, items: list) -> list:
    """items — список (id, название). Возвращает id пунктов, которые Gemini
    посчитал соответствующими тексту."""
    if not gemini_client or not items:
        return []
    numbered = "\n".join(f"{i + 1}. {name}" for i, (_, name) in enumerate(items))
    prompt = f"Список покупок:\n{numbered}\n\nТекст: {text}"
    try:
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=SHOPPING_MATCH_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=SHOPPING_MATCH_SCHEMA,
            ),
        )
        data = json.loads(response.text)
        numbers = data.get("matched_numbers", [])
        matched_ids = []
        for n in numbers:
            pos = int(n) - 1
            if 0 <= pos < len(items):
                matched_ids.append(items[pos][0])
        return matched_ids
    except Exception:
        logger.exception("Ошибка обращения к Gemini API (сверка списка покупок)")
        return []


NOMINATIVE_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "items": genai_types.Schema(
            type=genai_types.Type.ARRAY,
            items=genai_types.Schema(type=genai_types.Type.STRING),
        ),
    },
    required=["items"],
)

NOMINATIVE_INSTRUCTION = (
    "Тебе дан пронумерованный список названий товаров для покупки на русском "
    "языке — они могли попасть в любом падеже (например, после слова "
    "'купить'). Приведи каждое название к именительному падежу (словарной "
    "форме), сохрани порядок и ровно то же количество пунктов. Не меняй "
    "смысл, не переводи, не добавляй и не убирай пункты — только падеж/форму."
)


async def normalize_items_to_nominative(items: list) -> list:
    """Приводит названия пунктов списка покупок к именительному падежу через
    Gemini. При недоступности Gemini или несовпадении количества элементов
    возвращает исходный список без изменений."""
    if not gemini_client or not items:
        return items
    try:
        prompt = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(items))
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=NOMINATIVE_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=NOMINATIVE_SCHEMA,
            ),
        )
        data = json.loads(response.text)
        normalized = data.get("items", [])
        if len(normalized) == len(items):
            return [(n or "").strip() or orig for n, orig in zip(normalized, items)]
        return items
    except Exception:
        logger.exception("Ошибка обращения к Gemini API (нормализация списка покупок)")
        return items


def fmt(amount: float) -> str:
    return str(round(amount))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS budget (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            amount REAL NOT NULL,
            start_date TEXT NOT NULL,
            set_by TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            added_by TEXT,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS access_requests (
            chat_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            username TEXT,
            full_name TEXT,
            requested_at TEXT NOT NULL,
            decided_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS personal_budget (
            chat_id INTEGER PRIMARY KEY,
            amount REAL NOT NULL,
            start_date TEXT NOT NULL,
            display_name TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS period_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            day INTEGER NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )"""
    )
    for name in CATEGORIES:
        conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))

    conn.execute(
        """CREATE TABLE IF NOT EXISTS category_cache (
            description TEXT PRIMARY KEY,
            category_id INTEGER NOT NULL REFERENCES categories(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shopping_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            added_by TEXT,
            created_at TEXT NOT NULL
        )"""
    )

    # Миграция со старой схемы (отдельный бюджет/траты на каждый chat_id)
    # на общий семейный бюджет и общую историю трат.
    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "budgets" in tables:
        row = conn.execute(
            "SELECT amount, start_date FROM budgets ORDER BY start_date DESC LIMIT 1"
        ).fetchone()
        if row and not conn.execute("SELECT 1 FROM budget WHERE id = 1").fetchone():
            conn.execute(
                "INSERT INTO budget (id, amount, start_date, set_by) VALUES (1, ?, ?, NULL)",
                (row["amount"], row["start_date"]),
            )
        conn.execute("DROP TABLE budgets")

    expense_cols = {r["name"] for r in conn.execute("PRAGMA table_info(expenses)")}
    if "added_by" not in expense_cols:
        conn.execute("ALTER TABLE expenses ADD COLUMN added_by TEXT")

    if "category_id" not in expense_cols:
        conn.execute("ALTER TABLE expenses ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        if "category" in expense_cols:
            # Миграция со старой схемы, где категория хранилась текстом прямо
            # в expenses, на нормализованную category_id.
            for row in conn.execute(
                "SELECT DISTINCT category FROM expenses WHERE category IS NOT NULL"
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO categories (name) VALUES (?)", (row["category"],)
                )
            conn.execute(
                """UPDATE expenses SET category_id = (
                       SELECT id FROM categories WHERE categories.name = expenses.category
                   )
                   WHERE category IS NOT NULL"""
            )
            conn.execute("ALTER TABLE expenses DROP COLUMN category")

    cache_cols = {r["name"] for r in conn.execute("PRAGMA table_info(category_cache)")}
    if "category" in cache_cols and "category_id" not in cache_cols:
        conn.execute("ALTER TABLE category_cache ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        for row in conn.execute("SELECT DISTINCT category FROM category_cache"):
            conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (row["category"],))
        conn.execute(
            """UPDATE category_cache SET category_id = (
                   SELECT id FROM categories WHERE categories.name = category_cache.category
               )"""
        )
        conn.execute("ALTER TABLE category_cache DROP COLUMN category")

    conn.commit()
    conn.close()


def get_access_status(chat_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT status FROM access_requests WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    conn.close()
    return row["status"] if row else None


def create_access_request(chat_id: int, username: str, full_name: str):
    conn = get_conn()
    conn.execute(
        """INSERT INTO access_requests (chat_id, status, username, full_name, requested_at)
           VALUES (?, 'pending', ?, ?, ?)""",
        (chat_id, username, full_name, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def set_access_status(chat_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE access_requests SET status = ?, decided_at = ? WHERE chat_id = ?",
        (status, datetime.now().isoformat(), chat_id),
    )
    conn.commit()
    conn.close()


def list_pending_requests():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM access_requests WHERE status = 'pending' ORDER BY requested_at"
    ).fetchall()
    conn.close()
    return rows


def get_period_day() -> int:
    conn = get_conn()
    row = conn.execute("SELECT day FROM period_settings WHERE id = 1").fetchone()
    conn.close()
    return row["day"] if row else 1


def set_period_day(day: int):
    conn = get_conn()
    conn.execute(
        """INSERT INTO period_settings (id, day) VALUES (1, ?)
           ON CONFLICT(id) DO UPDATE SET day=excluded.day""",
        (day,),
    )
    conn.commit()
    conn.close()


def current_period_start() -> str:
    """Дата начала текущего отчётного периода: ближайшее (в прошлом или
    сегодня) число месяца, заданное как день начала периода."""
    day = get_period_day()
    now = datetime.now()

    def clamp_day(year: int, month: int) -> int:
        return min(day, calendar.monthrange(year, month)[1])

    candidate = now.replace(day=clamp_day(now.year, now.month), hour=0, minute=0, second=0, microsecond=0)
    if candidate > now:
        month = now.month - 1 or 12
        year = now.year - 1 if now.month == 1 else now.year
        candidate = datetime(year, month, clamp_day(year, month))
    return candidate.isoformat()


def set_personal_budget(chat_id: int, amount: float, display_name_: str = None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO personal_budget (chat_id, amount, start_date, display_name)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET
               amount=excluded.amount, display_name=excluded.display_name""",
        (chat_id, amount, datetime.now().isoformat(), display_name_),
    )
    conn.commit()
    conn.close()


def get_personal_budget(chat_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT amount, display_name FROM personal_budget WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    return row


def get_all_personal_budgets():
    conn = get_conn()
    rows = conn.execute(
        "SELECT chat_id, amount, display_name FROM personal_budget ORDER BY display_name"
    ).fetchall()
    conn.close()
    return rows


def get_shared_budget_total() -> float:
    conn = get_conn()
    row = conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM personal_budget").fetchone()
    conn.close()
    return row["total"]


def get_or_create_category_id(name: str) -> int:
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    conn.commit()
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["id"]


def get_categories() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT name FROM categories ORDER BY id").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def add_expense(chat_id: int, added_by: str, amount: float, description: str, category: str = None):
    category_id = get_or_create_category_id(category) if category else None
    conn = get_conn()
    conn.execute(
        "INSERT INTO expenses (chat_id, added_by, amount, description, created_at, category_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, added_by, amount, description, datetime.now().isoformat(), category_id),
    )
    conn.commit()
    conn.close()


def get_expenses(since: str = None):
    conn = get_conn()
    base_query = (
        "SELECT e.amount, e.description, e.added_by, e.created_at, c.name AS category "
        "FROM expenses e LEFT JOIN categories c ON e.category_id = c.id "
    )
    if since:
        rows = conn.execute(
            base_query + "WHERE e.created_at >= ? ORDER BY e.created_at",
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(base_query + "ORDER BY e.created_at").fetchall()
    conn.close()
    return rows


def get_cached_category(description: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT c.name AS category FROM category_cache cc "
        "JOIN categories c ON cc.category_id = c.id "
        "WHERE cc.description = ?",
        (description.strip().lower(),),
    ).fetchone()
    conn.close()
    return row["category"] if row else None


def cache_category(description: str, category: str):
    category_id = get_or_create_category_id(category)
    conn = get_conn()
    conn.execute(
        """INSERT INTO category_cache (description, category_id) VALUES (?, ?)
           ON CONFLICT(description) DO UPDATE SET category_id=excluded.category_id""",
        (description.strip().lower(), category_id),
    )
    conn.commit()
    conn.close()


def add_shopping_items(items: list, added_by: str):
    conn = get_conn()
    now = datetime.now().isoformat()
    for item in items:
        item = item.strip()
        if item:
            conn.execute(
                "INSERT INTO shopping_list (item, done, added_by, created_at) VALUES (?, 0, ?, ?)",
                (item, added_by, now),
            )
    conn.commit()
    conn.close()


def get_shopping_list():
    conn = get_conn()
    rows = conn.execute("SELECT id, item, done FROM shopping_list ORDER BY id").fetchall()
    conn.close()
    return rows


def clear_shopping_list_if_complete():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) AS c FROM shopping_list").fetchone()["c"]
    if total > 0:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM shopping_list WHERE done = 0"
        ).fetchone()["c"]
        if remaining == 0:
            conn.execute("DELETE FROM shopping_list")
            conn.commit()
    conn.close()


def _mark_shopping_ids_done(ids: list):
    if not ids:
        return
    conn = get_conn()
    for item_id in ids:
        conn.execute("UPDATE shopping_list SET done = 1 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    clear_shopping_list_if_complete()


async def mark_matching_shopping_items_done(text: str) -> list:
    """Ищет пункты списка покупок, упомянутые в тексте (описание траты либо
    произвольный список), и отмечает их выполненными. Возвращает названия
    отмеченных пунктов.

    Сначала пробует простое совпадение подстрок (быстро и бесплатно), а если
    ничего не нашлось — просит Gemini сопоставить по смыслу: одно и то же
    название может быть в разных падежах или расшифровано голосом чуть
    иначе («билевую верёвку» vs «бельевая верёвка»)."""
    text_lower = text.strip().lower()
    if not text_lower:
        return []

    conn = get_conn()
    rows = conn.execute("SELECT id, item FROM shopping_list WHERE done = 0").fetchall()
    conn.close()
    if not rows:
        return []

    matched_ids = []
    matched_names = []
    for r in rows:
        item_lower = r["item"].strip().lower()
        if item_lower and (item_lower in text_lower or text_lower in item_lower):
            matched_ids.append(r["id"])
            matched_names.append(r["item"])

    if not matched_ids and gemini_client:
        items = [(r["id"], r["item"]) for r in rows]
        gemini_ids = await match_shopping_items_with_gemini(text, items)
        id_to_name = {iid: item for iid, item in items}
        for item_id in gemini_ids:
            if item_id in id_to_name:
                matched_ids.append(item_id)
                matched_names.append(id_to_name[item_id])

    _mark_shopping_ids_done(matched_ids)
    return matched_names


async def mark_shopping_items_done_by_names(names: list) -> list:
    matched = []
    for name in names:
        matched.extend(await mark_matching_shopping_items_done(name))
    return matched


def get_spent_since(since: str) -> float:
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE created_at >= ?",
        (since,),
    ).fetchone()
    conn.close()
    return row["total"]


def get_spent_since_by_chat(chat_id: int, since: str) -> float:
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses "
        "WHERE chat_id = ? AND created_at >= ?",
        (chat_id, since),
    ).fetchone()
    conn.close()
    return row["total"]


def display_name(user) -> str:
    if user.first_name:
        return user.first_name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def is_admin(chat_id: int) -> bool:
    return ADMIN_CHAT_ID is not None and chat_id == ADMIN_CHAT_ID


def period_day_keyboard(selected_day: int = None) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for day in range(1, 32):
        label = f"•{day}•" if day == selected_day else str(day)
        row.append(InlineKeyboardButton(label, callback_data=f"period:{day}"))
        if len(row) == 7:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def setperiod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Команда доступна только администратору.")
        return
    current_day = get_period_day()
    await update.message.reply_text(
        f"Текущее число начала отчётного периода: {current_day}.\nВыберите новое:",
        reply_markup=period_day_keyboard(current_day),
    )


async def handle_period_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("Только администратор может это делать.", show_alert=True)
        return

    day = int(query.data.split(":")[1])
    set_period_day(day)
    period_start = datetime.fromisoformat(current_period_start()).strftime("%d.%m.%Y")
    await query.edit_message_text(
        f"Начало отчётного периода: {day}-е число каждого месяца.\n"
        f"Текущий период начался {period_start}.",
        reply_markup=period_day_keyboard(day),
    )
    await query.answer("Сохранено")


def access_request_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{chat_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"deny:{chat_id}"),
            ]
        ]
    )


ADMIN_CONTACT = "@aleksandr_kai"

FLYER_TEXT = (
    "👋 Это приватный семейный бот учёта расходов.\n\n"
    "Что умеет:\n"
    "• Записывает траты по тексту, голосом или по фото чека\n"
    "• Сам определяет категорию расхода\n"
    "• У каждого свой личный бюджет, общий — это их сумма\n"
    "• Общий список покупок с автосверкой при тратах\n"
    "• Подробные и краткие отчёты за день, период, по категориям\n\n"
    "Работает прямо на домашнем ПК — не нужно арендовать сервер и платить "
    "абонплату, всё бесплатно. Все данные под вашим контролем, никуда не "
    "передаются."
)

FLYER_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Хочу такого же себе", callback_data="want_own_bot")]]
)


async def handle_want_own_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        f"Пишите — помогу настроить: {ADMIN_CONTACT}"
    )


async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, разрешён ли чату доступ к боту.

    Если нет — сама обрабатывает сценарий (сообщает текущий статус заявки
    либо показывает рекламную листовку незнакомому пользователю) и
    возвращает False, чтобы вызывающий хендлер прервал обработку. Заявку на
    доступ к этому конкретному боту создаёт только команда /join — тут это
    не делается автоматически.
    """
    chat_id = update.effective_chat.id
    if is_admin(chat_id):
        return True

    status = get_access_status(chat_id)
    if status == "approved":
        return True
    if status == "pending":
        await update.effective_message.reply_text(
            "Ваша заявка на доступ ещё не одобрена администратором. Ожидайте решения."
        )
        return False
    if status == "denied":
        await update.effective_message.reply_text(
            "Доступ к этому боту отклонён администратором."
        )
        return False

    await update.effective_message.reply_text(FLYER_TEXT, reply_markup=FLYER_KEYBOARD)
    return False


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрытая команда — отправляет заявку на доступ к этому боту. Не
    зарегистрирована в меню и не упоминается в листовке."""
    chat_id = update.effective_chat.id
    if is_admin(chat_id):
        await update.message.reply_text("Вы администратор — доступ уже есть.")
        return

    status = get_access_status(chat_id)
    if status == "approved":
        await update.message.reply_text("У вас уже есть доступ.")
        return
    if status == "pending":
        await update.message.reply_text("Заявка уже отправлена, ожидайте решения.")
        return
    if status == "denied":
        await update.message.reply_text("Ваша заявка ранее была отклонена администратором.")
        return

    user = update.effective_user
    create_access_request(chat_id, user.username, user.first_name)
    await update.message.reply_text(
        "Заявка на доступ отправлена администратору. Как только её одобрят, "
        "вы сможете пользоваться ботом."
    )
    if ADMIN_CHAT_ID:
        who = display_name(user)
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"Новый запрос на доступ к боту от {who} (id {chat_id}).",
                reply_markup=access_request_keyboard(chat_id),
            )
        except Exception:
            logger.exception("Не удалось уведомить администратора о заявке")


async def handle_access_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("Только администратор может это делать.", show_alert=True)
        return

    action, chat_id_str = query.data.split(":")
    target_chat_id = int(chat_id_str)

    if action == "approve":
        set_access_status(target_chat_id, "approved")
        await query.edit_message_text(f"✅ Доступ одобрен для {target_chat_id}.")
        notify_text = "Ваша заявка одобрена! Можно пользоваться ботом — отправьте /start."
    else:
        set_access_status(target_chat_id, "denied")
        await query.edit_message_text(f"❌ Доступ отклонён для {target_chat_id}.")
        notify_text = "Ваша заявка на доступ отклонена администратором."

    try:
        await context.bot.send_message(target_chat_id, notify_text)
    except Exception:
        logger.exception("Не удалось уведомить пользователя о решении по заявке")

    await query.answer()


async def pending_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Команда доступна только администратору.")
        return

    rows = list_pending_requests()
    if not rows:
        await update.message.reply_text("Нет заявок в ожидании.")
        return

    for r in rows:
        who = r["full_name"] or (f"@{r['username']}" if r["username"] else str(r["chat_id"]))
        await update.message.reply_text(
            f"{who} (id {r['chat_id']}), заявка от {r['requested_at']}",
            reply_markup=access_request_keyboard(r["chat_id"]),
        )


HELP_TEXT = (
    "Это семейный бот учёта расходов. Каждый участник задаёт свой личный "
    "бюджет — общий бюджет и общий остаток считаются как сумма личных.\n\n"
    "Как вносить траты — любым способом:\n"
    "  • Текстом: 40 вода / 150 сок / 2х500 связь (количество × цена)\n"
    "    Можно несколько строк сразу — каждая станет отдельным расходом. "
    "Сложные фразы вроде «такси 350 и кофе 120» тоже распознаются.\n"
    "  • Голосом: просто скажите, что и за сколько купили — бот расшифрует "
    "и распознает сумму сам.\n"
    "  • Фото чека или скриншота оплаты — бот сам прочитает сумму и что куплено "
    "(в том числе несколько позиций сразу).\n\n"
    "Список покупок:\n"
    "  купить молоко, хлеб и яйца — добавить пункты (текстом или голосом, "
    "через запятую или «и»)\n"
    "  выполнено по списку молоко, хлеб — отметить пункты выполненными вручную\n"
    "При внесении любой траты (текст/голос/фото) бот сам сверяется со списком "
    "и вычёркивает совпавшие пункты. Когда всё куплено — список исчезает.\n\n"
    "Команды:\n"
    "/balance — общий остаток (сумма личных бюджетов минус все траты)\n"
    "/mybudget <сумма> — задать ваш личный бюджет\n"
    "/mybalance — остаток вашего личного бюджета\n"
    "/allbudgets — личные бюджеты и остатки всех участников\n"
    "/today — подробный список расходов за сегодня\n"
    "/expenses — подробный список расходов за отчётный период\n"
    "/summary — краткий отчёт по категориям и дням\n"
    "/categories — итоги по категориям за весь отчётный период\n"
    "/shopping — список запланированных покупок\n\n"
    "Отчётный период общий для всех и начинается заданного числа каждого месяца "
    "(настраивается администратором)."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    await update.message.reply_text(HELP_TEXT)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    total_budget = get_shared_budget_total()
    if total_budget == 0:
        await update.message.reply_text(
            "Общий бюджет пока не задан — никто не задал личный бюджет. "
            "Используйте /mybudget <сумма>."
        )
        return
    period_start = current_period_start()
    spent = get_spent_since(period_start)
    remaining = total_budget - spent
    period_str = datetime.fromisoformat(period_start).strftime("%d.%m.%Y")
    LABEL_WIDTH = 16
    lines = [
        f"{'Общий бюджет':<{LABEL_WIDTH}}{fmt(total_budget)}",
        f"{'Период с':<{LABEL_WIDTH}}{period_str}",
        f"{'Потрачено':<{LABEL_WIDTH}}{fmt(spent)}",
        f"{'Остаток':<{LABEL_WIDTH}}{fmt(remaining)}",
    ]
    table = "\n".join(lines)
    await update.message.reply_text(f"<pre>{table}</pre>", parse_mode=ParseMode.HTML)


async def mybudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    chat_id = update.effective_chat.id
    who = display_name(update.effective_user)
    if context.args:
        raw = context.args[0].replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            await update.message.reply_text(
                "Не удалось распознать сумму. Пример: /mybudget 10000"
            )
            return
        context.user_data.pop("awaiting_personal_budget", None)
        set_personal_budget(chat_id, amount, who)
        await update.message.reply_text(f"Личный бюджет: {fmt(amount)}.")
    else:
        context.user_data["awaiting_personal_budget"] = True
        await update.message.reply_text(
            "Введите сумму вашего личного бюджета:",
            reply_markup=CANCEL_KEYBOARD,
        )


async def mybalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    chat_id = update.effective_chat.id
    budget = get_personal_budget(chat_id)
    if not budget:
        await update.message.reply_text(
            "Личный бюджет ещё не задан. Используйте /mybudget <сумма>."
        )
        return
    period_start = current_period_start()
    spent = get_spent_since_by_chat(chat_id, period_start)
    remaining = budget["amount"] - spent
    period_str = datetime.fromisoformat(period_start).strftime("%d.%m.%Y")
    await update.message.reply_text(
        f"Личный бюджет: {fmt(budget['amount'])}\n"
        f"Отчётный период: с {period_str}\n"
        f"Потрачено лично: {fmt(spent)}\n"
        f"Остаток: {fmt(remaining)}"
    )


async def all_personal_budgets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return

    rows = get_all_personal_budgets()
    if not rows:
        await update.message.reply_text("Личные бюджеты пока никто не задавал.")
        return

    period_start = current_period_start()
    lines = []
    total_budget = 0.0
    total_spent = 0.0
    for r in rows:
        spent = get_spent_since_by_chat(r["chat_id"], period_start)
        remaining = r["amount"] - spent
        who = r["display_name"] or str(r["chat_id"])
        lines.append(
            f"{who} · бюджет {fmt(r['amount'])} · потрачено {fmt(spent)} · "
            f"остаток {fmt(remaining)}"
        )
        total_budget += r["amount"]
        total_spent += spent
    lines.append("")
    lines.append(
        f"Итого: бюджет {fmt(total_budget)} · потрачено {fmt(total_spent)} · "
        f"остаток {fmt(total_budget - total_spent)}"
    )

    await update.message.reply_text("\n".join(lines))


def build_expense_lines(rows) -> list:
    """Строки детальной таблицы расходов (Сумма/Описание/Кто) с разбивкой по
    дням — включая заголовок, разделитель и итог. Не объединяет в строку,
    чтобы вызывающий код мог разбить длинный отчёт на несколько сообщений."""
    TABLE_W = 34
    SUM_W, WHO_W = 5, 5
    DESC_W = TABLE_W - SUM_W - WHO_W - 2  # 2 разделителя-пробела между колонками

    header = f"{'Сумма':>{SUM_W}} {'Описание':<{DESC_W}} {'Кто':<{WHO_W}}"
    lines = [header, ""]
    total = 0.0
    prev_day = None
    for r in rows:
        created = datetime.fromisoformat(r["created_at"])
        if created.day != prev_day:
            if prev_day is not None:
                lines.append("")
            lines.append(f"{created.day} {MONTHS_GEN[created.month - 1]}")
            prev_day = created.day

        amount_str = fmt(r["amount"])
        desc = html.escape(r["description"])[: DESC_W - 2]
        who = html.escape(r["added_by"] or "?")[:WHO_W]
        lines.append(f"{amount_str:>{SUM_W}} {desc:<{DESC_W}} {who:<{WHO_W}}")
        total += r["amount"]
    lines.append("-" * TABLE_W)
    lines.append(f"Итого: {fmt(total)}")

    return lines


def render_expenses_table(rows) -> str:
    return "\n".join(build_expense_lines(rows))


# Реальный лимит длины текста сообщения в Telegram — 4096 символов. Оставляем
# запас на обёртку <pre></pre>, чтобы длинный отчёт не улетел с ошибкой
# "Message is too long", а автоматически разбился на несколько сообщений.
TELEGRAM_MESSAGE_LIMIT = 4000


async def send_lines_chunked(update: Update, lines: list):
    chunks = []
    current: list = []
    current_len = 0
    for line in lines:
        added_len = len(line) + 1  # + перевод строки
        if current and current_len + added_len > TELEGRAM_MESSAGE_LIMIT:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += added_len
    if current:
        chunks.append(current)

    for chunk in chunks:
        text = "\n".join(chunk)
        await update.message.reply_text(f"<pre>{text}</pre>", parse_mode=ParseMode.HTML)


async def today_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    today_start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    rows = get_expenses(today_start)

    if not rows:
        await update.message.reply_text("Сегодня расходов пока нет.")
        return

    await send_lines_chunked(update, build_expense_lines(rows))


async def expenses_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    since = current_period_start()
    rows = get_expenses(since)

    if not rows:
        await update.message.reply_text("Расходов за текущий период пока нет.")
        return

    await send_lines_chunked(update, build_expense_lines(rows))


def build_category_summary_lines(rows) -> list:
    """Краткий отчёт: по каждому дню — суммы по категориям и итог за день."""
    SUM_W = 6
    SEP_W = 24

    by_day = {}
    order = []
    for r in rows:
        day = datetime.fromisoformat(r["created_at"]).date()
        if day not in by_day:
            by_day[day] = {}
            order.append(day)
        category = r["category"] or "Другое"
        by_day[day][category] = by_day[day].get(category, 0.0) + r["amount"]

    lines = [f"{'Сумма':>{SUM_W}} Категория", ""]
    grand_total = 0.0
    for day in order:
        lines.append(f"{day.day} {MONTHS_GEN[day.month - 1].upper()}")
        cats = sorted(by_day[day].items(), key=lambda kv: -kv[1])
        day_total = 0.0
        for category, amount in cats:
            lines.append(f"{fmt(amount):>{SUM_W}} {html.escape(category)}")
            day_total += amount
        lines.append("-" * SEP_W)
        lines.append(f"{fmt(day_total):>{SUM_W}} Итого за день")
        grand_total += day_total
        lines.append("")
    lines.append("-" * SEP_W)
    lines.append(f"Итого: {fmt(grand_total)}")

    return lines


async def expenses_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    since = current_period_start()
    rows = get_expenses(since)

    if not rows:
        await update.message.reply_text("Расходов за текущий период пока нет.")
        return

    await send_lines_chunked(update, build_category_summary_lines(rows))


def build_category_totals_lines(rows) -> list:
    """Итоги по категориям за весь период (без разбивки по дням)."""
    SUM_W = 6
    SEP_W = 24

    totals = {}
    for r in rows:
        category = r["category"] or "Другое"
        totals[category] = totals.get(category, 0.0) + r["amount"]

    lines = [f"{'Сумма':>{SUM_W}} Категория", ""]
    grand_total = 0.0
    for category, amount in sorted(totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"{fmt(amount):>{SUM_W}} {html.escape(category)}")
        grand_total += amount
    lines.append("-" * SEP_W)
    lines.append(f"Итого: {fmt(grand_total)}")

    return lines


async def category_totals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    since = current_period_start()
    rows = get_expenses(since)

    if not rows:
        await update.message.reply_text("Расходов за текущий период пока нет.")
        return

    await send_lines_chunked(update, build_category_totals_lines(rows))


async def save_candidates_and_reply(
    update: Update,
    chat_id: int,
    who: str,
    candidates: list,
    failed: list,
    remove_keyboard: bool = False,
):
    """Сохраняет распознанные траты (с учётом лимита личного бюджета) и
    отправляет пользователю сводку. `candidates` — список (tag, amount,
    description), `failed` — нераспознанные фрагменты текста."""
    period_start = current_period_start()
    personal_budget = get_personal_budget(chat_id)
    remaining = None
    if personal_budget:
        remaining = personal_budget["amount"] - get_spent_since_by_chat(chat_id, period_start)

    accepted = []
    over_budget = []
    matched_shopping_items = []
    for tag, amount, description in candidates:
        if remaining is not None and amount > remaining:
            over_budget.append((tag, amount, description))
            continue
        category = await determine_category(description)
        add_expense(chat_id, who, amount, description, category)
        matched_shopping_items.extend(await mark_matching_shopping_items_done(description))
        accepted.append((tag, amount, description))
        if remaining is not None:
            remaining -= amount

    reply_lines = [f"{tag} {fmt(a)} — {d}" for tag, a, d in accepted]
    if failed:
        reply_lines.append("Не распознано: " + "; ".join(failed))
    if over_budget:
        reply_lines.append(
            "⛔ Превышает остаток вашего личного бюджета, не сохранено: "
            + "; ".join(f"{fmt(a)} — {d}" for _, a, d in over_budget)
        )
    if matched_shopping_items:
        reply_lines.append(
            "☑️ Отмечено в списке покупок: " + ", ".join(matched_shopping_items)
        )

    if accepted and remaining is not None:
        reply_lines.append(f"Остаток личного бюджета: {fmt(remaining)}")

    reply_markup = ReplyKeyboardRemove() if remove_keyboard else None
    if reply_lines:
        await update.message.reply_text("\n".join(reply_lines), reply_markup=reply_markup)
    elif remove_keyboard:
        await update.message.reply_text("Хорошо.", reply_markup=reply_markup)


# Голосовая расшифровка не всегда даёт идеально чистый текст — допускаем
# небольшое вступление перед триггерным словом ("так, купить...", "ещё
# купить...") и разные формы глагола ("купить"/"купи"/"купите").
_FILLER = r"(?:(?:так|ну|ещё|и ещё|надо|нужно|давай|окей|окай)[\s,]+)*"
BUY_PREFIX_RE = re.compile(
    rf"^{_FILLER}куп(?:ить|и|ите)\b[\s,:;\-–—]*(.+)$", re.IGNORECASE | re.DOTALL
)
DONE_LIST_PREFIX_RE = re.compile(
    rf"^{_FILLER}выполнено по списку\b[\s,:;\-–—]*(.+)$", re.IGNORECASE | re.DOTALL
)
# Разделители пунктов списка: запятая, перенос строки или отдельное слово "и"
# ("бельевую верёвку и бэйдж" — это два пункта, не один).
ITEM_SPLIT_RE = re.compile(r",|\n|\bи\b", re.IGNORECASE)


def split_items(text: str) -> list:
    return [i.strip(" .") for i in ITEM_SPLIT_RE.split(text) if i.strip(" .")]


async def handle_buy_message(update: Update, who: str, items_text: str):
    items = split_items(items_text)
    if not items:
        await update.message.reply_text("Не понял, что добавить в список покупок.")
        return
    items = await normalize_items_to_nominative(items)
    add_shopping_items(items, who)
    lines = "\n".join(f"• {html.escape(i)}" for i in items)
    await update.message.reply_text(
        f"Добавлено в список покупок:\n{lines}", parse_mode=ParseMode.HTML
    )


async def handle_done_list_message(update: Update, items_text: str):
    names = split_items(items_text)
    matched = await mark_shopping_items_done_by_names(names)
    if matched:
        await update.message.reply_text("Отмечено как выполнено: " + ", ".join(matched))
    else:
        await update.message.reply_text("Не нашёл совпадений в списке покупок.")


async def process_expense_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    who: str,
    text: str,
    remove_keyboard: bool = False,
):
    """Общая обработка сообщения (текстового или расшифрованного голосового):
    сначала проверяет команды списка покупок («купить …», «выполнено по
    списку …»), иначе пытается распознать траты."""
    stripped = text.strip()

    m = BUY_PREFIX_RE.match(stripped)
    if m:
        await handle_buy_message(update, who, m.group(1))
        return

    m = DONE_LIST_PREFIX_RE.match(stripped)
    if m:
        await handle_done_list_message(update, m.group(1))
        return

    saved = []
    failed = []
    for line in text.splitlines():
        parsed = parse_expense_line(line)
        if parsed:
            saved.append(parsed)
        elif line.strip():
            failed.append(line.strip())

    ai_saved = []
    if failed and gemini_client:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        ai_result = await parse_with_gemini("\n".join(failed))
        if ai_result:
            ai_saved = ai_result
            failed = []

    if not saved and not ai_saved and not failed:
        if remove_keyboard:
            await update.message.reply_text("Хорошо.", reply_markup=ReplyKeyboardRemove())
        return

    candidates = [("✅", a, d) for a, d in saved] + [("🤖", a, d) for a, d in ai_saved]
    await save_candidates_and_reply(
        update, chat_id, who, candidates, failed, remove_keyboard=remove_keyboard
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    chat_id = update.effective_chat.id
    who = display_name(update.effective_user)
    text = update.message.text or ""

    was_awaiting_budget = False
    if context.user_data.pop("awaiting_personal_budget", None):
        was_awaiting_budget = True
        if text.strip() == CANCEL_TEXT:
            await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
            return
        try:
            amount = float(text.replace(",", ".").strip())
        except ValueError:
            amount = None
        if amount is not None:
            set_personal_budget(chat_id, amount, who)
            await update.message.reply_text(
                f"Личный бюджет: {fmt(amount)}.", reply_markup=ReplyKeyboardRemove()
            )
            return
        # Не похоже на чистое число — скорее всего, это уже не ответ на запрос
        # бюджета, а обычный расход. Не блокируем пользователя, а пробуем
        # распознать сообщение ниже как обычно (клавиатуру всё равно снимем).

    await process_expense_text(
        update, context, chat_id, who, text, remove_keyboard=was_awaiting_budget
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return

    if is_admin(update.effective_chat.id) and (update.message.caption or "").strip().lower() == "icon":
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(os.path.join(BASE_DIR, "uploaded_icon.jpg"))
        await update.message.reply_text("Сохранил файл как uploaded_icon.jpg.")
        return

    if not gemini_client:
        await update.message.reply_text(
            "Распознавание изображений недоступно (не настроен Gemini)."
        )
        return

    chat_id = update.effective_chat.id
    who = display_name(update.effective_user)

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    result = await parse_image_with_gemini(image_bytes)
    if result is None:
        await update.message.reply_text(
            "Не удалось обработать изображение (ошибка распознавания)."
        )
        return
    if not result:
        await update.message.reply_text(
            "Не нашёл на изображении никаких трат — похоже, это не чек и не "
            "подтверждение платежа."
        )
        return

    candidates = [("🤖", a, d) for a, d in result]
    await save_candidates_and_reply(update, chat_id, who, candidates, [])


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    if not gemini_client:
        await update.message.reply_text(
            "Распознавание голосовых недоступно (не настроен Gemini)."
        )
        return

    context.user_data.pop("awaiting_personal_budget", None)

    chat_id = update.effective_chat.id
    who = display_name(update.effective_user)

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)
    audio_bytes = bytes(await tg_file.download_as_bytearray())

    text = await transcribe_voice_with_gemini(audio_bytes, mime_type=voice.mime_type or "audio/ogg")
    if text is None:
        await update.message.reply_text(
            "Не удалось обработать голосовое (ошибка распознавания)."
        )
        return
    if not text:
        await update.message.reply_text("Не расслышал ничего в этом голосовом.")
        return

    await process_expense_text(update, context, chat_id, who, text)


async def shopping_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    rows = get_shopping_list()
    if not rows:
        await update.message.reply_text("Нет запланированных покупок.")
        return
    lines = []
    for i, r in enumerate(rows, 1):
        item_text = f"{i}. {html.escape(r['item'])}"
        if r["done"]:
            item_text = f"<s>{item_text}</s>"
        lines.append(item_text)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def experimental_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Команда доступна только администратору.")
        return
    if not EXPERIMENTAL_REPORT_URL:
        await update.message.reply_text("Экспериментальный отчёт сейчас не настроен.")
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 Открыть отчёт", web_app=WebAppInfo(url=EXPERIMENTAL_REPORT_URL))]]
    )
    await update.message.reply_text(
        "Экспериментальный вид отчёта — адаптивная HTML-страница, открывается "
        "прямо внутри Telegram.",
        reply_markup=keyboard,
    )


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("today", "Расходы сегодня"),
            BotCommand("expenses", "Расходы за отчётный период"),
            BotCommand("summary", "По дням и категориям"),
            BotCommand("categories", "Итоги по категориям"),
            BotCommand("shopping", "Что купить"),
            BotCommand("balance", "Остаток"),
            BotCommand("mybalance", "Мой остаток"),
            BotCommand("allbudgets", "Личные бюджеты всех"),
            BotCommand("mybudget", "Задать личный бюджет"),
            BotCommand("start", "Помощь"),
        ]
    )
    if ADMIN_CHAT_ID:
        await app.bot.set_my_commands(
            [
                BotCommand("today", "Расходы сегодня"),
                BotCommand("expenses", "Расходы за отчётный период"),
                BotCommand("summary", "По дням и категориям"),
                BotCommand("categories", "Итоги по категориям"),
                BotCommand("shopping", "Что купить"),
                BotCommand("balance", "Остаток"),
                BotCommand("mybalance", "Мой остаток"),
                BotCommand("allbudgets", "Личные бюджеты всех"),
                BotCommand("mybudget", "Задать личный бюджет"),
                BotCommand("setperiod", "Начало периода"),
                BotCommand("pending", "Заявки на доступ"),
                BotCommand("start", "Помощь"),
                BotCommand("reportbeta", "🧪 Экспериментально: Отчёт"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID),
        )


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан (проверьте файл .env)")

    init_db()

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("mybudget", mybudget))
    app.add_handler(CommandHandler("mybalance", mybalance))
    app.add_handler(CommandHandler("allbudgets", all_personal_budgets))
    app.add_handler(CommandHandler("setperiod", setperiod_cmd))
    app.add_handler(CommandHandler("today", today_expenses))
    app.add_handler(CommandHandler("expenses", expenses_list))
    app.add_handler(CommandHandler("summary", expenses_summary))
    app.add_handler(CommandHandler("categories", category_totals))
    app.add_handler(CommandHandler("shopping", shopping_list_cmd))
    app.add_handler(CommandHandler("pending", pending_requests))
    app.add_handler(CommandHandler("reportbeta", experimental_report_cmd))
    app.add_handler(CommandHandler("join", join_cmd))
    app.add_handler(CallbackQueryHandler(handle_access_decision, pattern=r"^(approve|deny):"))
    app.add_handler(CallbackQueryHandler(handle_period_selection, pattern=r"^period:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_want_own_bot, pattern=r"^want_own_bot$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.run_polling()


if __name__ == "__main__":
    main()
