from os import getenv
import asyncio
import re
import traceback
from datetime import date, timedelta

import aiohttp
from aiohttp_socks import ProxyConnector
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputRichMessage,
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = getenv("BOT_TOKEN")
PROXY_URL = getenv("PROXY_URL", "socks5://kan:Parol_12@130.49.146.248:443")

GROUP_ID = 99
API_URL = "https://ruz.guz.ru/api/schedule/group/{group}"

RU_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

FULL_DAYS = {
    "Пн": "Понедельник",
    "Вт": "Вторник",
    "Ср": "Среда",
    "Чт": "Четверг",
    "Пт": "Пятница",
    "Сб": "Суббота",
    "Вс": "Воскресенье",
}

DAY_BUTTON_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})\s*\((\w{2})\)$")

# состояние: chat_id -> понедельник отображаемой недели
user_week: dict[int, date] = {}

dp = Dispatcher()
router = Router()
dp.include_router(router)


# ---------- состояние недели ----------

def current_monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def get_user_monday(chat_id: int) -> date:
    return user_week.get(chat_id, current_monday())


def set_user_monday(chat_id: int, monday: date) -> None:
    user_week[chat_id] = monday


# ---------- API ----------

async def fetch_schedule(group_id: int, start: str, finish: str) -> list:
    url = API_URL.format(group=group_id)
    params = {"start": start, "finish": finish, "lng": 1}
    timeout = aiohttp.ClientTimeout(total=20)

    connector = ProxyConnector.from_url(PROXY_URL)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()


# ---------- Форматирование (Rich HTML) ----------

def format_lessons_for_day(lessons: list, iso_date: str) -> str:
    # iso_date приходит как YYYY-MM-DD, выводим как DD.MM.YYYY
    yyyy, mm, dd = iso_date.split("-")
    pretty_date = f"{dd}.{mm}.{yyyy}"

    if not lessons:
        return f"<p><b>📅 Занятий нет</b> — {pretty_date}</p>"

    short_day = lessons[0].get("dayOfWeekString", "")
    full_day = FULL_DAYS.get(short_day, short_day)

    grp = ""
    for l in lessons:
        g = l.get("subGroup") or l.get("stream") or (l.get("listGroups") or [{}])[0].get("group")
        if g:
            grp = g
            break

    html = f"<h3>📅 {full_day} ({pretty_date})</h3>"
    if grp:
        html += f"<p>Группа {grp}</p>"

    html += "<table border='1'>"
    html += (
        "<tr>"
        "<th align='left'>Время</th>"
        "<th align='left'>Занятие</th>"
        "<th align='left'>Информация</th>"
        "</tr>"
    )

    for l in sorted(lessons, key=lambda x: x["beginLesson"]):
        time_s = f"{l['beginLesson']}–{l['endLesson']}"
        name_s = l["discipline"]
        loc_s = f"{l['auditorium']} ({l['building']})"
        info_s = f"{l['kindOfWork']}, {l['lecturer']}"

        html += (
            "<tr>"
            f"<td>{time_s}</td>"
            f"<td><b>{name_s}</b><br/>{loc_s}</td>"
            f"<td>{info_s}</td>"
            "</tr>"
        )

    html += "</table>"
    return html


def split_message(text: str, limit: int = 30000) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)]


# ---------- Клавиатура ----------

def build_days_keyboard(monday: date) -> ReplyKeyboardMarkup:
    keyboard = []
    row = []
    for i in range(6):
        d = monday + timedelta(days=i)
        label = f"{d.strftime('%d.%m.%Y')} ({RU_DAYS[i]})"
        row.append(KeyboardButton(text=label))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # кнопки листания — внизу
    keyboard.append([KeyboardButton(text="<<"), KeyboardButton(text=">>")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выбери день или листай недели",
    )


async def show_week(message: Message, monday: date) -> None:
    sunday = monday + timedelta(days=6)
    kb = build_days_keyboard(monday)
    await message.answer(
        f"Неделя {monday.strftime('%d.%m.%Y')} — {sunday.strftime('%d.%m.%Y')}:",
        reply_markup=kb,
    )


# ---------- Хендлеры ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я показываю расписание.\n\n"
        "Команды:\n"
        "/schedule — показать дни недели\n\n"
        "Кнопки << и >> листают недели."
    )


@router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    monday = current_monday()
    set_user_monday(message.chat.id, monday)
    await show_week(message, monday)


@router.message(F.text == "<<")
async def on_prev_week(message: Message):
    monday = get_user_monday(message.chat.id) - timedelta(days=7)
    set_user_monday(message.chat.id, monday)
    await show_week(message, monday)


@router.message(F.text == ">>")
async def on_next_week(message: Message):
    monday = get_user_monday(message.chat.id) + timedelta(days=7)
    set_user_monday(message.chat.id, monday)
    await show_week(message, monday)


@router.message(lambda m: m.text and DAY_BUTTON_RE.match(m.text))
async def on_day_press(message: Message):
    m = DAY_BUTTON_RE.match(message.text)
    dd, mm, yyyy, _ = m.groups()
    api_date = f"{yyyy}.{mm}.{dd}"
    iso_date = f"{yyyy}-{mm}-{dd}"

    await message.answer(f"⏳ Загружаю {dd}.{mm}.{yyyy}…")

    try:
        lessons = await fetch_schedule(GROUP_ID, api_date, api_date)
    except aiohttp.ClientError as e:
        await message.answer(f"❌ Ошибка сети: {e}")
        return
    except Exception as e:
        traceback.print_exc()
        await message.answer(f"❌ {type(e).__name__}: {e!r}")
        return

    day_lessons = [l for l in lessons if l.get("date") == iso_date]

    html = format_lessons_for_day(day_lessons, iso_date)
    for chunk in split_message(html):
        await message.answer_rich(
            rich_message=InputRichMessage(html=chunk)
        )


@router.message()
async def fallback(message: Message):
    await message.answer("Неизвестная команда. Попробуй /schedule")


# ---------- Точка входа ----------

async def main():
    bot = Bot(token=TOKEN)
    print("start")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())