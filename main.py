import asyncio
import html
import os
from datetime import datetime
from typing import Any

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
HH_USER_AGENT = os.getenv("HH_USER_AGENT", "JobResponseAgent/0.1 (your_email@example.com)")
HH_AREA = os.getenv("HH_AREA", "2")
HH_PERIOD_DAYS = int(os.getenv("HH_PERIOD_DAYS", "1"))
HH_PER_PAGE = int(os.getenv("HH_PER_PAGE", "5"))

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Проверь файл .env")

router = Router()

HH_API_URL = "https://api.hh.ru/vacancies"

# Кэш найденных вакансий.
# Нужен, чтобы кнопки могли понимать, с какой вакансией работаем.
VACANCIES_CACHE: dict[str, dict[str, Any]] = {}


POSITIVE_KEYWORDS = [
    "руководитель производства",
    "начальник производства",
    "директор производства",
    "операционный директор",
    "coo",
    "fmcg",
    "бытовая химия",
    "косметика",
    "производство",
    "склад",
    "логистика",
    "1с",
    "erp",
    "бережливое производство",
    "lean",
    "маркировка",
    "честный знак",
    "качество",
    "себестоимость",
    "оптимизация",
    "команда",
]

STOP_KEYWORDS = [
    "продавец",
    "менеджер по продажам",
    "торговый представитель",
    "вахта",
    "оператор линии",
    "кладовщик",
    "рабочий",
    "без опыта",
    "кассир",
]


def vacancy_keyboard(vacancy_id: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Откликнуться",
                    callback_data=f"apply:{vacancy_id}",
                ),
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data=f"skip:{vacancy_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить письмо",
                    callback_data=f"edit_letter:{vacancy_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Открыть вакансию",
                    url=url,
                )
            ],
        ]
    )


def format_salary(salary: dict[str, Any] | None) -> str:
    if not salary:
        return "не указана"

    salary_from = salary.get("from")
    salary_to = salary.get("to")
    currency = salary.get("currency", "")

    currency_map = {
        "RUR": "₽",
        "RUB": "₽",
        "USD": "$",
        "EUR": "€",
    }

    currency_sign = currency_map.get(currency, currency)

    if salary_from and salary_to:
        return f"{salary_from:,}–{salary_to:,} {currency_sign}".replace(",", " ")

    if salary_from:
        return f"от {salary_from:,} {currency_sign}".replace(",", " ")

    if salary_to:
        return f"до {salary_to:,} {currency_sign}".replace(",", " ")

    return "не указана"


def format_published_at(value: str | None) -> str:
    if not value:
        return "не указано"

    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def build_text_for_scoring(item: dict[str, Any]) -> str:
    title = item.get("name") or ""
    employer = (item.get("employer") or {}).get("name") or ""
    area = (item.get("area") or {}).get("name") or ""

    snippet = item.get("snippet") or {}
    requirement = snippet.get("requirement") or ""
    responsibility = snippet.get("responsibility") or ""

    return f"{title} {employer} {area} {requirement} {responsibility}".lower()


def score_vacancy(item: dict[str, Any]) -> tuple[int, list[str]]:
    text = build_text_for_scoring(item)

    score = 45
    reasons: list[str] = []

    for keyword in POSITIVE_KEYWORDS:
        if keyword.lower() in text:
            score += 5
            reasons.append(keyword)

    for keyword in STOP_KEYWORDS:
        if keyword.lower() in text:
            score -= 15

    score = max(0, min(score, 98))

    if not reasons:
        reasons = [
            "подходит по названию вакансии",
            "требует ручной проверки описания",
        ]

    return score, reasons[:6]


def choose_resume_variant(item: dict[str, Any]) -> str:
    text = build_text_for_scoring(item)

    if "операционный директор" in text or "coo" in text:
        return "Операционный директор / COO"

    if "erp" in text or "1с" in text or "автоматизац" in text:
        return "Руководитель проектов автоматизации / ERP"

    if "склад" in text or "логист" in text:
        return "Руководитель производственно-складской логистики"

    return "Руководитель производства / COO"


def generate_cover_letter(vacancy: dict[str, Any]) -> str:
    title = vacancy["title"]
    company = vacancy["company"]

    return (
        f"Здравствуйте! Меня заинтересовала вакансия «{title}»"
        f"{' в компании ' + company if company != 'не указана' else ''}. "
        "У меня более 13 лет опыта в производстве FMCG, запуске и масштабировании "
        "производственных процессов, управлении командами, внедрении 1С ERP, "
        "контроле себестоимости, качества и производительности. "
        "Готов обсудить, как мой опыт может быть полезен вашей компании."
    )


def normalize_hh_vacancy(item: dict[str, Any]) -> dict[str, Any]:
    hh_id = str(item.get("id", "unknown"))
    vacancy_id = f"hh_{hh_id}"

    employer = item.get("employer") or {}
    area = item.get("area") or {}

    score, reasons = score_vacancy(item)

    vacancy = {
        "id": vacancy_id,
        "hh_id": hh_id,
        "source": "HH.ru",
        "title": item.get("name") or "Без названия",
        "company": employer.get("name") or "не указана",
        "city": area.get("name") or "не указан",
        "salary": format_salary(item.get("salary")),
        "published": format_published_at(item.get("published_at")),
        "url": item.get("alternate_url") or item.get("apply_alternate_url") or "https://hh.ru/",
        "apply_url": item.get("apply_alternate_url"),
        "score": score,
        "resume_variant": choose_resume_variant(item),
        "reason": reasons,
    }

    vacancy["cover_letter"] = generate_cover_letter(vacancy)

    return vacancy


def format_vacancy(vacancy: dict[str, Any]) -> str:
    reasons = "\n".join([f"— {html.escape(item)}" for item in vacancy["reason"]])

    return f"""
🔥 <b>Новая вакансия</b>

<b>{html.escape(vacancy["title"])}</b>
Компания: {html.escape(vacancy["company"])}
Город: {html.escape(vacancy["city"])}
Зарплата: {html.escape(vacancy["salary"])}
Источник: {html.escape(vacancy["source"])}
Опубликована: {html.escape(vacancy["published"])}

<b>Оценка совпадения:</b> {vacancy["score"]}%

<b>Почему подходит:</b>
{reasons}

<b>Рекомендуемое резюме:</b>
{html.escape(vacancy["resume_variant"])}

<b>Сопроводительное письмо:</b>
{html.escape(vacancy["cover_letter"])}
""".strip()


def log_action(action: str, vacancy_id: str, user_id: int) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    line = f"{now}; user_id={user_id}; action={action}; vacancy_id={vacancy_id}\n"

    with open("actions_log.txt", "a", encoding="utf-8") as file:
        file.write(line)


async def fetch_hh_vacancies(query: str) -> list[dict[str, Any]]:
    headers = {
        "HH-User-Agent": HH_USER_AGENT,
        "Accept": "application/json",
    }

    params = {
        "text": query,
        "area": HH_AREA,
        "period": HH_PERIOD_DAYS,
        "per_page": HH_PER_PAGE,
        "page": 0,
        "order_by": "publication_time",
    }

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(HH_API_URL, params=params) as response:
            response_text = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"Ошибка HH API: HTTP {response.status}. "
                    f"User-Agent: {HH_USER_AGENT}. "
                    f"Ответ: {response_text[:500]}"
                )

            data = await response.json()

    items = data.get("items", [])

    vacancies = [normalize_hh_vacancy(item) for item in items]

    vacancies.sort(key=lambda item: item["score"], reverse=True)

    return vacancies


def get_find_query(message: Message) -> str:
    if not message.text:
        return "руководитель производства"

    parts = message.text.split(maxsplit=1)

    if len(parts) == 1:
        return "руководитель производства"

    query = parts[1].strip()

    return query or "руководитель производства"


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    text = """
Привет! Я бот для быстрых откликов на вакансии.

Команды:

/find — найти свежие вакансии на HH.ru
/find директор производства — поиск по конкретной фразе
/find операционный директор — другой поисковый запрос

Пока бот:
— ищет свежие вакансии;
— оценивает совпадение;
— предлагает резюме;
— готовит сопроводительное письмо;
— ждёт твоё подтверждение.

Реальную отправку отклика подключим следующим этапом через авторизацию HH.
""".strip()

    await message.answer(text)


@router.message(Command("find"))
async def find_vacancies_handler(message: Message) -> None:
    query = get_find_query(message)

    await message.answer(
        f"🔎 Ищу свежие вакансии на HH.ru по запросу:\n<b>{html.escape(query)}</b>"
    )

    try:
        vacancies = await fetch_hh_vacancies(query)
    except Exception as error:
        await message.answer(
            f"""
⚠️ Не получилось получить вакансии с HH.ru.

Ошибка:
<code>{html.escape(str(error))}</code>

Частые причины:
— не указан HH_USER_AGENT в .env;
— нет интернета;
— HH временно не отвечает;
— запрос заблокирован из-за некорректного User-Agent.
""".strip()
        )
        return

    if not vacancies:
        await message.answer(
            """
Новых вакансий не найдено.

Попробуй шире:
/find начальник производства
/find директор производства
/find операционный директор
/find руководитель склада
""".strip()
        )
        return

    await message.answer(f"Нашёл вакансий: {len(vacancies)}. Показываю лучшие варианты.")

    for vacancy in vacancies:
        VACANCIES_CACHE[vacancy["id"]] = vacancy

        await message.answer(
            format_vacancy(vacancy),
            reply_markup=vacancy_keyboard(
                vacancy_id=vacancy["id"],
                url=vacancy["url"],
            ),
        )


@router.callback_query(F.data.startswith("apply:"))
async def apply_handler(callback: CallbackQuery) -> None:
    vacancy_id = callback.data.split(":", 1)[1]
    vacancy = VACANCIES_CACHE.get(vacancy_id)

    log_action(
        action="apply",
        vacancy_id=vacancy_id,
        user_id=callback.from_user.id,
    )

    await callback.answer("Отклик подтверждён")

    if not vacancy:
        await callback.message.answer(
            "✅ Действие зафиксировано, но вакансия не найдена в кэше. Запусти /find ещё раз."
        )
        return

    await callback.message.answer(
        f"""
✅ Отклик зафиксирован.

Вакансия: <b>{html.escape(vacancy["title"])}</b>
Компания: {html.escape(vacancy["company"])}
Резюме: {html.escape(vacancy["resume_variant"])}

Пока это не отправка на HH, а фиксация твоего решения.
Следующий этап — подключить OAuth HH и реальный отклик.
""".strip()
    )


@router.callback_query(F.data.startswith("skip:"))
async def skip_handler(callback: CallbackQuery) -> None:
    vacancy_id = callback.data.split(":", 1)[1]
    vacancy = VACANCIES_CACHE.get(vacancy_id)

    log_action(
        action="skip",
        vacancy_id=vacancy_id,
        user_id=callback.from_user.id,
    )

    await callback.answer("Вакансия пропущена")

    title = vacancy["title"] if vacancy else vacancy_id

    await callback.message.answer(
        f"⏭ Вакансия пропущена: {html.escape(title)}"
    )


@router.callback_query(F.data.startswith("edit_letter:"))
async def edit_letter_handler(callback: CallbackQuery) -> None:
    vacancy_id = callback.data.split(":", 1)[1]
    vacancy = VACANCIES_CACHE.get(vacancy_id)

    log_action(
        action="edit_letter",
        vacancy_id=vacancy_id,
        user_id=callback.from_user.id,
    )

    await callback.answer("Редактирование письма")

    if not vacancy:
        await callback.message.answer(
            "Вакансия не найдена в кэше. Запусти /find ещё раз."
        )
        return

    await callback.message.answer(
        f"""
✏️ Текущее письмо:

{html.escape(vacancy["cover_letter"])}

Позже сделаем режим:
1. Нажал «Изменить письмо»
2. Написал новый текст в чат
3. Бот сохранил его для этого отклика
""".strip()
    )


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    print("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())