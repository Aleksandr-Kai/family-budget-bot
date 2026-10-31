import asyncio
import calendar
import contextvars
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
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

# DeepSeek — запасной (или, если задано в .env, основной) провайдер для
# текстовых ИИ-задач (разбор трат, категории, сверка списка покупок и
# т.п.). Голос и фото распознаёт только Gemini — у DeepSeek нет мультимодальных
# моделей, поэтому transcribe_voice_with_gemini/parse_image_with_gemini
# всегда идут напрямую в Gemini, без выбора провайдера.
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# Какой провайдер пробовать первым для текстовых задач: "gemini" (по
# умолчанию) или "deepseek". Второй, если для него задан ключ, используется
# как запасной — автоматически, без дополнительной настройки.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

# Пауз между повторными попытками при перегрузке Gemini (503) — сама
# перегрузка почти всегда временная (со стороны Google), поэтому вместо
# немедленного отказа ждём и пробуем снова, пока не кончатся попытки.
GEMINI_RETRY_DELAYS = [5, 15, 30, 60, 60]

# Устанавливается в main() после создания Application — нужен, чтобы
# отправить уведомление о повторных попытках из generate_content_with_retry,
# куда chat_id/context не прокидываются explicitно (см. current_chat_id).
telegram_bot = None

# ContextVar вместо явного параметра — так все вложенные Gemini-вызовы внутри
# обработки одного сообщения (парсинг, категория, сверка списка покупок и
# т.д.) знают, кому писать, без изменения сигнатур всех промежуточных
# функций. Выставляется в начале handle_text/handle_photo/handle_voice.
current_chat_id: contextvars.ContextVar = contextvars.ContextVar(
    "current_chat_id", default=None
)

_last_retry_notice: dict = {}
RETRY_NOTICE_DEBOUNCE_SECONDS = 20

# Пока не ноль — Gemini упёрся в лимит запросов (HTTP 429, RESOURCE_EXHAUSTED)
# и все вызовы должны сразу отказывать без похода в сеть, до этого момента
# (time.monotonic()). Лимит минутный (free tier), поэтому отдельно от 503 —
# здесь нет смысла повторять попытку раньше, чем Google сам разрешит.
_llm_cooldown_until = 0.0


class LLMRateLimited(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Gemini: превышен лимit запросов, повтор через {retry_after:.0f}с")


def _extract_retry_delay(details) -> float:
    """Достаёт retryDelay (в секундах) из тела ошибки 429, если Google его
    прислал; иначе разумное значение по умолчанию."""
    try:
        for d in details.get("error", {}).get("details", []):
            if d.get("@type", "").endswith("RetryInfo"):
                delay = d.get("retryDelay", "")
                if delay.endswith("s"):
                    return float(delay[:-1])
    except Exception:
        pass
    return 60.0


async def _notify_rate_limited(retry_after: float):
    chat_id = current_chat_id.get()
    if chat_id is None or telegram_bot is None:
        return
    now = time.monotonic()
    if now - _last_retry_notice.get(chat_id, 0) < RETRY_NOTICE_DEBOUNCE_SECONDS:
        return
    _last_retry_notice[chat_id] = now
    try:
        await telegram_bot.send_message(
            chat_id,
            f"🚦 Gemini достиг лимита запросов в минуту. Подожду {int(retry_after) + 1}с, "
            "пока лимит сбросится — до этого момента ИИ-распознавание временно недоступно, "
            "попробуйте отправить сообщение ещё раз чуть позже.",
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление о лимите Gemini")


async def _notify_retry_in_progress():
    """Сообщает пользователю, что запрос не потерян — просто Gemini сейчас
    недоступен и бот пробует снова. С debounce, чтобы при нескольких
    параллельных Gemini-вызовах (например, категории для нескольких позиций
    чека) не прислать несколько одинаковых уведомлений подряд."""
    chat_id = current_chat_id.get()
    if chat_id is None or telegram_bot is None:
        return
    now = time.monotonic()
    if now - _last_retry_notice.get(chat_id, 0) < RETRY_NOTICE_DEBOUNCE_SECONDS:
        return
    _last_retry_notice[chat_id] = now
    try:
        await telegram_bot.send_message(
            chat_id,
            "⏳ Gemini сейчас перегружен и не отвечает. Запрос не потерян — "
            "пробую ещё раз, сообщу, как только обработается.",
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление о повторной попытке")


async def generate_content_with_retry(**kwargs):
    """Обёртка над gemini_client.aio.models.generate_content.

    При HTTP 503 (модель перегружена) — повторяет попытку с паузами.
    При HTTP 429 (лимит запросов исчерпан) — не повторяет вообще: включает
    общий кулдаун на указанное Google время, и все вызовы (в том числе из
    других обработчиков) до его истечения сразу отказывают без обращения к
    сети. Любая другая ошибка пробрасывается сразу."""
    global _llm_cooldown_until
    now = time.monotonic()
    if now < _llm_cooldown_until:
        remaining = _llm_cooldown_until - now
        await _notify_rate_limited(remaining)
        raise LLMRateLimited(remaining)

    delays = [0] + GEMINI_RETRY_DELAYS
    for i, delay in enumerate(delays):
        if delay:
            logger.warning("Gemini вернул 503, повтор через %sс (попытка %s/%s)", delay, i + 1, len(delays))
            await _notify_retry_in_progress()
            await asyncio.sleep(delay)
        try:
            return await gemini_client.aio.models.generate_content(**kwargs)
        except genai_errors.ClientError as e:
            if e.code != 429:
                raise
            retry_after = _extract_retry_delay(e.details)
            _llm_cooldown_until = time.monotonic() + retry_after
            logger.warning("Gemini: лимит запросов исчерпан, кулдаун на %.0fс", retry_after)
            await _notify_rate_limited(retry_after)
            raise LLMRateLimited(retry_after)
        except genai_errors.ServerError as e:
            if e.code != 503 or i == len(delays) - 1:
                raise
            continue


async def _call_gemini_json(system_instruction: str, user_content: str, schema):
    """Текстовый запрос к Gemini со структурированным JSON-ответом по
    заданной схеме. None — провайдер недоступен (нет ключа) или любая
    ошибка вызова (включая кулдаун/лимит — уже залогированы внутри
    generate_content_with_retry)."""
    if not gemini_client:
        return None
    try:
        response = await generate_content_with_retry(
            model=GEMINI_MODEL,
            contents=user_content,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        return json.loads(response.text)
    except Exception:
        logger.exception("Ошибка обращения к Gemini API")
        return None


async def _call_deepseek_json(system_instruction: str, user_content: str):
    """Текстовый запрос к DeepSeek (OpenAI-совместимый chat/completions) с
    режимом гарантированного JSON-ответа. В отличие от Gemini, DeepSeek не
    умеет принудительно следовать схеме — формат описывается текстом в
    system_instruction. None — нет ключа или любая ошибка (сеть, невалидный
    JSON и т.п.)."""
    if not DEEPSEEK_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                DEEPSEEK_URL,
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": user_content},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception:
        logger.exception("Ошибка обращения к DeepSeek API")
        return None


async def ask_llm_json(system_instruction: str, user_content: str, gemini_schema, deepseek_format_hint: str):
    """Текстовая ИИ-задача с основным и запасным провайдером.

    Пробует LLM_PROVIDER первым, затем — второго (если для него настроен
    ключ). Возвращает разобранный JSON (dict) от того, кто первым ответил
    успешно, или None, если недоступны оба (вызывающий код в этом случае
    ведёт себя так, будто ИИ вообще нет — как было до DeepSeek)."""
    deepseek_instruction = (
        f"{system_instruction}\n\nОтветь строго в виде JSON: {deepseek_format_hint}"
    )
    callers = {
        "gemini": lambda: _call_gemini_json(system_instruction, user_content, gemini_schema),
        "deepseek": lambda: _call_deepseek_json(deepseek_instruction, user_content),
    }
    order = [LLM_PROVIDER] + [p for p in callers if p != LLM_PROVIDER]
    for provider in order:
        result = await callers[provider]()
        if result is not None:
            return result
    return None

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID")) if os.getenv("ADMIN_CHAT_ID") else None

# Временный адрес туннеля для экспериментального HTML-отчёта (Mini App).
# Меняется при перезапуске cloudflared — тогда нужно обновить .env.
EXPERIMENTAL_REPORT_URL = os.getenv("EXPERIMENTAL_REPORT_URL")
# Mini App (webapp_server.py) — отдельный туннель, отдельная переменная.
EXPERIMENTAL_MINIAPP_URL = os.getenv("EXPERIMENTAL_MINIAPP_URL")

CANCEL_TEXT = "Отмена"
CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[CANCEL_TEXT]], resize_keyboard=True, one_time_keyboard=True
)

# Главное меню — постоянная клавиатура вместо списка слэш-команд. Показана
# после /start и восстанавливается после временных клавиатур (отмена ввода
# бюджета и т.п.), чтобы не пропадала.
MENU_TODAY = "📅 Расходы сегодня"
MENU_SUMMARY = "📋 Расходы за период"
MENU_CATEGORIES = "📊 Итоги по категориям"
MENU_SHOPPING = "🛒 Список покупок"
MENU_BUDGET = "💰 Бюджет"
MENU_SET_BUDGET = "✏️ Задать личный бюджет"

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [MENU_TODAY, MENU_SUMMARY],
        [MENU_CATEGORIES, MENU_SHOPPING],
        [MENU_BUDGET, MENU_SET_BUDGET],
    ],
    resize_keyboard=True,
)

# Кнопка "Убрать выполненное" — инлайн-кнопка под списком покупок (не
# ломает главное меню-клавиатуру), появляется только если есть вычёркнутые
# пункты.
REMOVE_DONE_CALLBACK = "shopping_clear_done"
REMOVE_DONE_BUTTON_TEXT = "🧹 Убрать выполненное"

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

MONTHS_NOM = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
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


def _extract_expenses_from_json(data: dict):
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


EXPENSE_FORMAT_HINT = '{"expenses": [{"amount": число, "description": "строка"}]}'


async def parse_with_gemini(text: str):
    """Пытается распознать траты в сложном свободном тексте через ИИ
    (основной и запасной провайдер, см. LLM_PROVIDER/DEEPSEEK_API_KEY).

    Возвращает список (amount, description) или None, если оба провайдера
    недоступны (нет ключей, ошибка API и т.п.) — тогда вызывающий код
    должен вести себя так, будто ИИ-интеграции нет.
    """
    data = await ask_llm_json(GEMINI_SYSTEM_INSTRUCTION, text, EXPENSE_SCHEMA, EXPENSE_FORMAT_HINT)
    if data is None:
        return None
    return _extract_expenses_from_json(data)


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
        response = await generate_content_with_retry(
            model=GEMINI_MODEL,
            contents=[image_part],
            config=genai_types.GenerateContentConfig(
                system_instruction=GEMINI_IMAGE_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=EXPENSE_SCHEMA,
            ),
        )
        return _extract_expenses_from_json(json.loads(response.text))
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
        response = await generate_content_with_retry(
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


CATEGORIES = ["Продукты", "Кафе", "Транспорт", "Дом", "Здоровье", "Личное", "Собака", "Связь", "Другое"]

# Порядок важен: более специфичные категории проверяются раньше общей
# "Продукты", иначе случайное совпадение подстроки (например, "рис" внутри
# "Рисовый сендвич") перетягивает результат не туда.
CATEGORY_KEYWORDS = {
    "Связь": [
        "связь", "интернет", "мобильн", "тариф", "мегаком", "o!", "beeline",
        "билайн",
    ],
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


CATEGORY_FORMAT_HINT = '{"category": "ровно одна из: ' + ", ".join(CATEGORIES) + '"}'


async def classify_category_with_gemini(description: str) -> str:
    data = await ask_llm_json(CATEGORY_SYSTEM_INSTRUCTION, description, CATEGORY_SCHEMA, CATEGORY_FORMAT_HINT)
    if data is None:
        return "Другое"
    category = data.get("category")
    return category if category in CATEGORIES else "Другое"


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


SHOPPING_MATCH_FORMAT_HINT = '{"matched_numbers": [число, ...]}'


async def match_shopping_items_with_gemini(text: str, items: list) -> list:
    """items — список (id, название). Возвращает id пунктов, которые ИИ
    посчитал соответствующими тексту."""
    if not items:
        return []
    numbered = "\n".join(f"{i + 1}. {name}" for i, (_, name) in enumerate(items))
    prompt = f"Список покупок:\n{numbered}\n\nТекст: {text}"
    data = await ask_llm_json(
        SHOPPING_MATCH_INSTRUCTION, prompt, SHOPPING_MATCH_SCHEMA, SHOPPING_MATCH_FORMAT_HINT
    )
    if data is None:
        return []
    numbers = data.get("matched_numbers", [])
    matched_ids = []
    for n in numbers:
        try:
            pos = int(n) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= pos < len(items):
            matched_ids.append(items[pos][0])
    return matched_ids


# Быстрый локальный путь для самых очевидных случаев — чтобы не дёргать
# Gemini на каждое "удали из списка выполненные".
DELETE_ALL_DONE_WORDS = {
    "выполненные", "выполненное", "выполненный", "выполненных", "выполнено",
}

DELETE_ALL_DONE_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "is_all_done": genai_types.Schema(type=genai_types.Type.BOOLEAN),
    },
    required=["is_all_done"],
)

DELETE_ALL_DONE_INSTRUCTION = (
    "Пользователь пишет боту учёта покупок фразу после слов 'удали из "
    "списка'. Определи: это просьба удалить ЦЕЛИКОМ все уже "
    "купленные/выполненные пункты списка (например 'выполненные', "
    "'купленное', 'купленные вещи', 'то что уже купили', 'готовое') — тогда "
    "is_all_done = true. Если названы конкретные товары для удаления "
    "(например 'молоко и хлеб') — is_all_done = false."
)


DELETE_ALL_DONE_FORMAT_HINT = '{"is_all_done": true/false}'


async def is_delete_all_done_request(text: str) -> bool:
    normalized = text.strip(" .").lower()
    if normalized in DELETE_ALL_DONE_WORDS:
        return True
    data = await ask_llm_json(
        DELETE_ALL_DONE_INSTRUCTION, text, DELETE_ALL_DONE_SCHEMA, DELETE_ALL_DONE_FORMAT_HINT
    )
    if data is None:
        return False
    return bool(data.get("is_all_done", False))


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


NOMINATIVE_FORMAT_HINT = '{"items": ["строка", ...]} — ровно столько же пунктов, в том же порядке'


async def normalize_items_to_nominative(items: list) -> list:
    """Приводит названия пунктов списка покупок к именительному падежу через
    ИИ. При недоступности обоих провайдеров или несовпадении количества
    элементов возвращает исходный список без изменений."""
    if not items:
        return items
    prompt = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(items))
    data = await ask_llm_json(NOMINATIVE_INSTRUCTION, prompt, NOMINATIVE_SCHEMA, NOMINATIVE_FORMAT_HINT)
    if data is None:
        return items
    normalized = data.get("items", [])
    if len(normalized) == len(items):
        return [(n or "").strip() or orig for n, orig in zip(normalized, items)]
    return items


# Фразы-триггеры (BUY_PREFIX_RE/DONE_LIST_PREFIX_RE/DELETE_PREFIX_RE,
# определены ниже) ловят только фиксированный порядок слов ("удали из
# списка сыр"). Пользователь может сказать иначе ("удали сыр из списка
# покупок") — ту же команду, но в свободном порядке. Если сообщение
# содержит явный намёк на список покупок (корень "списк", "куплен",
# "купил", "выполнен"), но не совпало ни с одним жёстким шаблоном —
# спрашиваем ИИ, что имелось в виду, вместо того чтобы требовать от
# пользователя запоминать точную формулировку.
SHOPPING_INTENT_HINT_RE = re.compile(r"спис[оа]?к|куплен|купил|выполнен", re.IGNORECASE)

SHOPPING_INTENT_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "intent": genai_types.Schema(
            type=genai_types.Type.STRING,
            enum=["add", "done", "delete", "delete_all_done", "none"],
        ),
        "items": genai_types.Schema(
            type=genai_types.Type.ARRAY, items=genai_types.Schema(type=genai_types.Type.STRING)
        ),
    },
    required=["intent", "items"],
)

SHOPPING_INTENT_INSTRUCTION = (
    "Пользователь пишет боту учёта расходов и списка покупок сообщение, которое "
    "может (а может и не) быть командой по списку покупок — в любом порядке слов "
    "и свободной формулировке, не только стандартными фразами. Определи intent:\n"
    "- 'add' — добавить пункт(ы) в список покупок (например 'купить хлеб', "
    "'добавь в список молоко', 'нужно ещё взять сыр')\n"
    "- 'done' — отметить пункт(ы) как выполненные/купленные, не удаляя из списка "
    "(например 'выполнено молоко', 'хлеб куплен', 'я купила сыр')\n"
    "- 'delete' — удалить конкретные пункт(ы) из списка, независимо от порядка слов "
    "(например 'удали сыр из списка покупок', 'убери из списка хлеб', "
    "'сыр больше не нужен в списке')\n"
    "- 'delete_all_done' — удалить ВСЕ уже выполненные пункты целиком (например "
    "'удали из списка всё выполненное', 'убери купленное из списка')\n"
    "- 'none' — сообщение вообще не про список покупок (например обычная трата "
    "вроде '150 хлеб', или не относится к списку покупок)\n"
    "В items верни названия товаров, упомянутых в сообщении (пустой список для "
    "'delete_all_done' и 'none')."
)

SHOPPING_INTENT_FORMAT_HINT = '{"intent": "add|done|delete|delete_all_done|none", "items": ["строка", ...]}'


async def classify_shopping_intent(text: str):
    """Если сообщение похоже на команду по списку покупок (есть характерный
    корень), но не совпало с жёсткими фразами-триггерами — спрашивает ИИ,
    что имелось в виду. Возвращает dict {"intent": ..., "items": [...]}
    или None, если явно не похоже на команду по списку / ИИ недоступен."""
    if not SHOPPING_INTENT_HINT_RE.search(text):
        return None
    data = await ask_llm_json(
        SHOPPING_INTENT_INSTRUCTION, text, SHOPPING_INTENT_SCHEMA, SHOPPING_INTENT_FORMAT_HINT
    )
    if not data or data.get("intent") not in ("add", "done", "delete", "delete_all_done"):
        return None
    return data


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


def get_broadcast_chat_ids() -> list:
    """Все чаты, которым можно писать рассылкой: одобренные пользователи
    плюс администратор (у него доступ не через access_requests)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT chat_id FROM access_requests WHERE status = 'approved'"
    ).fetchall()
    conn.close()
    chat_ids = {r["chat_id"] for r in rows}
    if ADMIN_CHAT_ID is not None:
        chat_ids.add(ADMIN_CHAT_ID)
    return list(chat_ids)


async def broadcast_message(text: str, reply_markup=None, parse_mode=None):
    if telegram_bot is None:
        return
    for chat_id in get_broadcast_chat_ids():
        try:
            await telegram_bot.send_message(
                chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        except Exception:
            logger.exception("Не удалось отправить рассылку в чат %s", chat_id)


async def notify_if_shopping_list_cleared():
    """Если список покупок только что полностью опустел — сообщает об этом
    всем рассылкой. Кнопка "Список покупок" теперь часть главного меню и
    всегда на месте, её не трогаем."""
    if shopping_list_is_empty():
        await broadcast_message("✅ Список покупок пуст.")


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
    previous = get_personal_budget(chat_id)
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
    logger.info(
        "Изменение личного бюджета: chat_id=%s, кто=%s, было=%s, стало=%s",
        chat_id,
        display_name_,
        previous["amount"] if previous else None,
        amount,
    )


def get_personal_budget(chat_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT amount, display_name FROM personal_budget WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    return row


def resolve_budget_amount(chat_id: int, raw: str):
    """Разбирает введённую сумму личного бюджета.

    Если строка начинается с '+' или '-', это изменение уже заданного
    бюджета на эту величину (например "+500" добавит 500 к текущему).
    Иначе — новая абсолютная сумма, как раньше.

    Возвращает (amount, error): amount — None, если строка вообще не похожа
    на число (вызывающий код сам решает, что делать дальше); error — текст
    ошибки, если число распозналось, но применить его нельзя (изменение без
    текущего бюджета)."""
    raw = raw.strip()
    # "- 4000" (с пробелом после знака) — тоже коррекция на -4000, а не
    # нераспознанная строка: float() сам по себе пробел не прощает.
    raw = re.sub(r"^([+-])\s+", r"\1", raw)
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None, None

    if raw[:1] in ("+", "-"):
        current = get_personal_budget(chat_id)
        if not current:
            return None, "Личный бюджет ещё не задан — сначала укажите точную сумму, без + или -."
        return current["amount"] + value, None

    return value, None


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
    # Выполненные (вычёркнутые) пункты показываем первыми — так все
    # отмеченные покупки видно сразу сверху, а не вперемешку с остальными.
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, item, done FROM shopping_list ORDER BY done DESC, id"
    ).fetchall()
    conn.close()
    return rows


def shopping_clear_done_markup(rows=None):
    """Инлайн-кнопка "Убрать выполненное" под списком покупок — только если
    есть вычёркнутые пункты, иначе None (не трогает главное меню-клавиатуру)."""
    if rows is None:
        rows = get_shopping_list()
    if not any(r["done"] for r in rows):
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(REMOVE_DONE_BUTTON_TEXT, callback_data=REMOVE_DONE_CALLBACK)]]
    )


def shopping_list_is_empty() -> bool:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) AS c FROM shopping_list").fetchone()["c"]
    conn.close()
    return total == 0


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


def delete_done_shopping_items() -> list:
    """Удаляет только уже выполненные (зачёркнутые) пункты, не трогая
    оставшиеся. Возвращает названия удалённых пунктов."""
    conn = get_conn()
    rows = conn.execute("SELECT item FROM shopping_list WHERE done = 1").fetchall()
    names = [r["item"] for r in rows]
    if names:
        conn.execute("DELETE FROM shopping_list WHERE done = 1")
        conn.commit()
    conn.close()
    return names


def _mark_shopping_ids_done(ids: list):
    if not ids:
        return
    conn = get_conn()
    for item_id in ids:
        conn.execute("UPDATE shopping_list SET done = 1 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def _delete_shopping_ids(ids: list):
    if not ids:
        return
    conn = get_conn()
    for item_id in ids:
        conn.execute("DELETE FROM shopping_list WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


async def find_matching_shopping_items(text: str) -> list:
    """Ищет невыполненные пункты списка покупок, соответствующие тексту
    (описание траты либо произвольный список). Возвращает список (id, item).

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

    matched = []
    for r in rows:
        item_lower = r["item"].strip().lower()
        if item_lower and (item_lower in text_lower or text_lower in item_lower):
            matched.append((r["id"], r["item"]))

    if not matched and gemini_client:
        items = [(r["id"], r["item"]) for r in rows]
        gemini_ids = await match_shopping_items_with_gemini(text, items)
        id_to_name = {iid: item for iid, item in items}
        matched = [(iid, id_to_name[iid]) for iid in gemini_ids if iid in id_to_name]

    return matched


async def mark_matching_shopping_items_done(text: str) -> list:
    """Пункты списка покупок, соответствующие купленному товару, вычёркиваются
    (не удаляются) — остаются видны в списке, наверху. Если после этого все
    пункты выполнены, список очищается целиком."""
    matches = await find_matching_shopping_items(text)
    _mark_shopping_ids_done([iid for iid, _ in matches])
    if matches:
        clear_shopping_list_if_complete()
    return [name for _, name in matches]


async def mark_shopping_items_done_by_names(names: list) -> list:
    matched = []
    for name in names:
        matched.extend(await mark_matching_shopping_items_done(name))
    return matched


async def delete_matching_shopping_items(text: str) -> list:
    matches = await find_matching_shopping_items(text)
    _delete_shopping_ids([iid for iid, _ in matches])
    return [name for _, name in matches]


async def delete_shopping_items_by_names(names: list) -> list:
    deleted = []
    for name in names:
        deleted.extend(await delete_matching_shopping_items(name))
    return deleted


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
    "  выполнено молоко, хлеб / удали сыр из списка покупок — отметить "
    "выполненным или удалить. Формулировка не обязана быть точной — "
    "понимает и обычный порядок слов, не только стандартные фразы.\n"
    "  удали из списка выполненные — убрать только уже зачёркнутые пункты\n"
    "При внесении любой траты (текст/голос/фото) бот сам сверяется со списком "
    "и вычёркивает совпавшие пункты. Когда всё куплено — список исчезает.\n\n"
    "Кнопки ниже — быстрый доступ к расходам, категориям, списку покупок и "
    "бюджету. Значок рядом с полем ввода открывает мини-приложение — там же "
    "статистика по месяцам и (для администратора) настройка отчётного периода."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


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


BUDGET_INPUT_TIMEOUT_SECONDS = 60


async def _cancel_budget_wait_after_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Если пользователь не ответил на запрос суммы бюджета за отведённое
    время — снимаем режим ожидания сами, чтобы случайное сообщение позже
    не было принято за ввод бюджета."""
    await asyncio.sleep(BUDGET_INPUT_TIMEOUT_SECONDS)
    if not context.user_data.get("awaiting_personal_budget"):
        return
    context.user_data.pop("awaiting_personal_budget", None)
    try:
        await context.bot.send_message(
            chat_id,
            "⌛ Время на ввод суммы бюджета истекло, отменено.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
    except Exception:
        logger.exception("Не удалось отправить уведомление об истечении таймаута ввода бюджета")


async def mybudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    chat_id = update.effective_chat.id
    who = display_name(update.effective_user)
    if context.args:
        amount, error = resolve_budget_amount(chat_id, context.args[0])
        if error:
            await update.message.reply_text(error)
            return
        if amount is None:
            await update.message.reply_text(
                "Не удалось распознать сумму. Примеры: /mybudget 10000, /mybudget +500, /mybudget -300"
            )
            return
        context.user_data.pop("awaiting_personal_budget", None)
        set_personal_budget(chat_id, amount, who)
        await update.message.reply_text(f"Личный бюджет: {fmt(amount)}.")
    else:
        context.user_data["awaiting_personal_budget"] = True
        asyncio.create_task(_cancel_budget_wait_after_timeout(chat_id, context))
        await update.message.reply_text(
            "Введите сумму вашего личного бюджета (можно со знаком + или -, "
            "чтобы изменить текущий бюджет на эту величину):",
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


def build_monthly_category_lines(rows) -> list:
    """По каждому календарному месяцу (за всё время) — суммы по категориям
    и итог за месяц."""
    SUM_W = 6
    SEP_W = 24

    by_month = {}
    order = []
    for r in rows:
        dt = datetime.fromisoformat(r["created_at"])
        key = (dt.year, dt.month)
        if key not in by_month:
            by_month[key] = {}
            order.append(key)
        category = r["category"] or "Другое"
        by_month[key][category] = by_month[key].get(category, 0.0) + r["amount"]

    lines = [f"{'Сумма':>{SUM_W}} Категория", ""]
    grand_total = 0.0
    for year, month in order:
        lines.append(f"{MONTHS_NOM[month - 1].upper()} {year}")
        cats = sorted(by_month[(year, month)].items(), key=lambda kv: -kv[1])
        month_total = 0.0
        for category, amount in cats:
            lines.append(f"{fmt(amount):>{SUM_W}} {html.escape(category)}")
            month_total += amount
        lines.append("-" * SEP_W)
        lines.append(f"{fmt(month_total):>{SUM_W}} Итого за месяц")
        grand_total += month_total
        lines.append("")
    lines.append("-" * SEP_W)
    lines.append(f"Итого: {fmt(grand_total)}")

    return lines


async def monthly_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    rows = get_expenses()

    if not rows:
        await update.message.reply_text("Расходов пока нет.")
        return

    await send_lines_chunked(update, build_monthly_category_lines(rows))


async def save_candidates_and_reply(
    update: Update,
    chat_id: int,
    who: str,
    candidates: list,
    failed: list,
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
    for tag, amount, description in candidates:
        if remaining is not None and amount > remaining:
            over_budget.append((tag, amount, description))
            continue
        accepted.append((tag, amount, description))
        if remaining is not None:
            remaining -= amount

    # Категории и сверка со списком покупок дёргают Gemini — делаем это для
    # всех принятых позиций параллельно, а не по очереди, иначе чек из
    # нескольких новых товаров обрабатывается минутами.
    categories = await asyncio.gather(
        *(determine_category(description) for _, _, description in accepted)
    )
    shopping_matches = await asyncio.gather(
        *(mark_matching_shopping_items_done(description) for _, _, description in accepted)
    )
    matched_shopping_items = []
    for (tag, amount, description), category, matches in zip(accepted, categories, shopping_matches):
        add_expense(chat_id, who, amount, description, category)
        matched_shopping_items.extend(matches)

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

    if reply_lines:
        await update.message.reply_text("\n".join(reply_lines))

    if matched_shopping_items:
        await notify_if_shopping_list_cleared()


# Голосовая расшифровка не всегда даёт идеально чистый текст — допускаем
# небольшое вступление перед триггерным словом ("так, купить...", "ещё
# купить...") и разные формы глагола ("купить"/"купи"/"купите").
_FILLER = r"(?:(?:так|ну|ещё|и ещё|надо|нужно|давай|окей|окай)[\s,]+)*"
BUY_PREFIX_RE = re.compile(
    rf"^{_FILLER}куп(?:ить|и|ите)\b[\s,:;\-–—]*(.+)$", re.IGNORECASE | re.DOTALL
)
DONE_LIST_PREFIX_RE = re.compile(
    rf"^{_FILLER}(?:выполнено по списку|куплено|купил(?:а)?)\b[\s,:;\-–—]*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
DELETE_PREFIX_RE = re.compile(
    rf"^{_FILLER}удали(?:ть)? из списка\b[\s,:;\-–—]*(.+)$", re.IGNORECASE | re.DOTALL
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
    await broadcast_message(
        f"🛒 {who} добавил(а) в список покупок:\n{lines}",
        parse_mode=ParseMode.HTML,
    )


async def handle_done_list_message(update: Update, items_text: str):
    names = split_items(items_text)
    matched = await mark_shopping_items_done_by_names(names)
    if matched:
        await update.message.reply_text("Отмечено как выполнено: " + ", ".join(matched))
        await notify_if_shopping_list_cleared()
    else:
        await update.message.reply_text("Не нашёл совпадений в списке покупок.")


async def remove_done_shopping_items_and_reply(update: Update):
    deleted = delete_done_shopping_items()
    if deleted:
        await update.message.reply_text("Удалены выполненные пункты: " + ", ".join(deleted))
        await notify_if_shopping_list_cleared()
    else:
        await update.message.reply_text("Выполненных пунктов пока нет.")


async def handle_delete_from_shopping_message(update: Update, items_text: str):
    if await is_delete_all_done_request(items_text):
        await remove_done_shopping_items_and_reply(update)
        return

    names = split_items(items_text)
    deleted = await delete_shopping_items_by_names(names)
    if deleted:
        await update.message.reply_text("Удалено из списка покупок: " + ", ".join(deleted))
        await notify_if_shopping_list_cleared()
    else:
        await update.message.reply_text("Не нашёл совпадений в списке покупок.")


async def process_expense_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    who: str,
    text: str,
):
    """Общая обработка сообщения (текстового или расшифрованного голосового):
    сначала проверяет команды списка покупок по фиксированным фразам
    («купить …», «выполнено по списку …»), затем — свободной формулировкой
    через ИИ (см. classify_shopping_intent), и только если это вообще не
    про список покупок — пытается распознать траты."""
    stripped = text.strip()

    m = BUY_PREFIX_RE.match(stripped)
    if m:
        await handle_buy_message(update, who, m.group(1))
        return

    m = DONE_LIST_PREFIX_RE.match(stripped)
    if m:
        await handle_done_list_message(update, m.group(1))
        return

    m = DELETE_PREFIX_RE.match(stripped)
    if m:
        await handle_delete_from_shopping_message(update, m.group(1))
        return

    intent_data = await classify_shopping_intent(stripped)
    if intent_data:
        intent = intent_data["intent"]
        items_text = ", ".join(intent_data.get("items") or [])
        if intent == "add" and items_text:
            await handle_buy_message(update, who, items_text)
            return
        if intent == "done" and items_text:
            await handle_done_list_message(update, items_text)
            return
        if intent == "delete" and items_text:
            await handle_delete_from_shopping_message(update, items_text)
            return
        if intent == "delete_all_done":
            await remove_done_shopping_items_and_reply(update)
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
    if failed and (gemini_client or DEEPSEEK_API_KEY):
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        ai_result = await parse_with_gemini("\n".join(failed))
        if ai_result:
            ai_saved = ai_result
            failed = []

    if not saved and not ai_saved and not failed:
        return

    candidates = [("✅", a, d) for a, d in saved] + [("🤖", a, d) for a, d in ai_saved]
    await save_candidates_and_reply(update, chat_id, who, candidates, failed)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    chat_id = update.effective_chat.id
    current_chat_id.set(chat_id)
    who = display_name(update.effective_user)
    text = update.message.text or ""

    menu_actions = {
        MENU_TODAY: today_expenses,
        MENU_SUMMARY: expenses_summary,
        MENU_CATEGORIES: category_totals,
        MENU_SHOPPING: shopping_list_cmd,
        MENU_BUDGET: all_personal_budgets,
        MENU_SET_BUDGET: mybudget,
    }
    action = menu_actions.get(text.strip())
    if action:
        await action(update, context)
        return

    if context.user_data.get("awaiting_personal_budget"):
        if text.strip() == CANCEL_TEXT:
            context.user_data.pop("awaiting_personal_budget", None)
            await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU_KEYBOARD)
            return
        amount, error = resolve_budget_amount(chat_id, text)
        if amount is not None:
            context.user_data.pop("awaiting_personal_budget", None)
            set_personal_budget(chat_id, amount, who)
            await update.message.reply_text(
                f"Личный бюджет: {fmt(amount)}.", reply_markup=MAIN_MENU_KEYBOARD
            )
            return
        # Пока не введут число или не нажмут "Отмена" — остаёмся в режиме
        # ожидания суммы бюджета. Ни в коем случае не пытаемся распознать
        # ввод как расход, иначе опечатка в сумме бюджета попадёт в траты.
        await update.message.reply_text(
            error or "Не удалось распознать сумму. Введите число (можно со знаком "
            "+ или -, чтобы изменить текущий бюджет) или нажмите «Отмена».",
            reply_markup=CANCEL_KEYBOARD,
        )
        return

    await process_expense_text(update, context, chat_id, who, text)


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
    current_chat_id.set(chat_id)
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
    current_chat_id.set(chat_id)
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
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=shopping_clear_done_markup(rows)
    )


async def handle_shopping_clear_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    deleted = delete_done_shopping_items()
    if deleted:
        await query.edit_message_text("Удалены выполненные пункты: " + ", ".join(deleted))
        await notify_if_shopping_list_cleared()
    else:
        await query.edit_message_text("Выполненных пунктов пока нет.")
    await query.answer()


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


async def experimental_miniapp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Команда доступна только администратору.")
        return
    if not EXPERIMENTAL_MINIAPP_URL:
        await update.message.reply_text("Мини-приложение сейчас не настроено.")
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Открыть мини-приложение", web_app=WebAppInfo(url=EXPERIMENTAL_MINIAPP_URL))]]
    )
    await update.message.reply_text(
        "Прототип мини-приложения — дашборд, расходы, категории, список покупок.",
        reply_markup=keyboard,
    )


async def post_init(app: Application):
    # Основная навигация теперь — кнопки главного меню (см.
    # MAIN_MENU_KEYBOARD) и мини-приложение, а не список слэш-команд.
    # Команды оставлены минимально — как резерв для тех, кто печатает их
    # руками, плюс специальные админские, которым нет места среди кнопок.
    await app.bot.set_my_commands([BotCommand("start", "Помощь / главное меню")])
    if ADMIN_CHAT_ID:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Помощь / главное меню"),
                BotCommand("pending", "Заявки на доступ"),
                BotCommand("reportbeta", "🧪 Экспериментально: Отчёт"),
                BotCommand("miniapp", "Открыть мини-приложение"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_CHAT_ID),
        )

    # Кнопка меню (значок рядом с полем ввода) открывает мини-приложение
    # напрямую, а не список команд — если URL не настроен (например, в
    # локальной отладке без туннеля), откатываемся на обычное меню команд.
    if EXPERIMENTAL_MINIAPP_URL:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Открыть", web_app=WebAppInfo(url=EXPERIMENTAL_MINIAPP_URL)
            )
        )
    else:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonDefault())


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан (проверьте файл .env)")

    init_db()

    # По умолчанию PTB обрабатывает обновления по одному за раз, поэтому
    # долгий запрос к Gemini (голосовое, фото чека) от одного пользователя
    # блокирует ответы на команды других — включаем параллельную обработку.
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    global telegram_bot
    telegram_bot = app.bot

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("mybudget", mybudget))
    app.add_handler(CommandHandler("mybalance", mybalance))
    app.add_handler(CommandHandler("allbudgets", all_personal_budgets))
    app.add_handler(CommandHandler("today", today_expenses))
    app.add_handler(CommandHandler("expenses", expenses_list))
    app.add_handler(CommandHandler("summary", expenses_summary))
    app.add_handler(CommandHandler("categories", category_totals))
    app.add_handler(CommandHandler("months", monthly_stats))
    app.add_handler(CommandHandler("shopping", shopping_list_cmd))
    app.add_handler(CommandHandler("pending", pending_requests))
    app.add_handler(CommandHandler("reportbeta", experimental_report_cmd))
    app.add_handler(CommandHandler("miniapp", experimental_miniapp_cmd))
    app.add_handler(CommandHandler("join", join_cmd))
    app.add_handler(CallbackQueryHandler(handle_access_decision, pattern=r"^(approve|deny):"))
    app.add_handler(CallbackQueryHandler(handle_want_own_bot, pattern=r"^want_own_bot$"))
    app.add_handler(
        CallbackQueryHandler(handle_shopping_clear_done_callback, pattern=rf"^{REMOVE_DONE_CALLBACK}$")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.run_polling()


if __name__ == "__main__":
    main()
