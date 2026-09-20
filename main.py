from os import getenv
import asyncio
import re
import traceback
from datetime import date, timedelta

import aiohttp
from aiohttp_socks import ProxyConnector
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = getenv("BOT_TOKEN")
PROXY_URL = getenv("PROXY_URL", "socks5://kan:Parol_12@130.49.146.248:443")

GROUP_ID = 99
API_URL = "https://ruz.guz.ru/api/schedule/group/{group}"

RU_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAY_BUTTON_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})\s*\((\w{2})\)$")

dp = Dispatcher()
router = Router()
dp.include_router(router)


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


# ---------- Форматирование таблицы ----------

def _wrap(text: str, width: int) -> list[str]:
    """Разбивает строку по словам так, чтобы каждая часть влезала в width."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + (1 if cur else 0) + len(w) <= width:
            cur = f"{cur} {w}".strip()
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _pad(s: str, width: int) -> str:
    """Дополняет строку пробелами справа до width (с обрезкой, если длиннее)."""
    s = s[:width]
    return s + " " * (width - len(s))


def format_lessons_for_day(lessons: list, iso_date: str) -> str:
    if not lessons:
        return f"📅 {iso_date} — занятий нет."

    day_name = lessons[0].get("dayOfWeekString", "")

    # группа/поток для шапки (если есть)
    grp = ""
    for l in lessons:
        g = l.get("subGroup") or l.get("stream") or (l.get("listGroups") or [{}])[0].get("group")
        if g:
            grp = g
            break

    header = f"📅 <b>{day_name}, {iso_date}</b>"
    if grp:
        header += f" · {grp}"

    W1, W2, W3 = 13, 30, 24  # ширины колонок (без учёта рамок)

    top  = "┌" + "─" * (W1 + 2) + "┬" + "─" * (W2 + 2) + "┬" + "─" * (W3 + 2) + "┐"
    mid  = "├" + "─" * (W1 + 2) + "┼" + "─" * (W2 + 2) + "┼" + "─" * (W3 + 2) + "┤"
    bot  = "└" + "─" * (W1 + 2) + "┴" + "─" * (W2 + 2) + "┴" + "─" * (W3 + 2) + "┘"
    hrow = f"│ {_pad('Время', W1)} │ {_pad('Занятие', W2)} │ {_pad('Информация', W3)} │"

    rows = [top, hrow, mid]

    sorted_lessons = sorted(lessons, key=lambda x: x["beginLesson"])
    last_idx = len(sorted_lessons) - 1

    for idx, l in enumerate(sorted_lessons):
        time_s = f"{l['beginLesson']}–{l['endLesson']}"
        name_s = l["discipline"]
        loc_s  = f"{l['auditorium']} ({l['building']})"
        info_s = f"{l['kindOfWork']}, {l['lecturer']}"

        c1 = _wrap(time_s, W1)
        c2 = _wrap(name_s, W2) + _wrap(loc_s, W2)
        c3 = _wrap(info_s, W3)

        n = max(len(c1), len(c2), len(c3))
        for i in range(n):
            a = c1[i] if i < len(c1) else ""
            b = c2[i] if i < len(c2) else ""
            c = c3[i] if i < len(c3) else ""
            rows.append(f"│ {_pad(a, W1)} │ {_pad(b, W2)} │ {_pad(c, W3)} │")

        rows.append(bot if idx == last_idx else mid)

    table = "\n".join(rows)
    return f"{header}\n<pre>{table}</pre>"


def split_message(text: str, limit: int = 4000) -> list[str]:
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

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выбери день",
    )


# ---------- Хендлеры ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я показываю расписание.\n\n"
        "Команды:\n"
        "/schedule — показать кнопки с днями"
    )


@router.message(Command("schedule"))
async def cmd_schedule(message: Message):
    today = date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    sunday = monday + timedelta(days=6)

    kb = build_days_keyboard(monday)
    await message.answer(
        f"Выбери день ({monday.strftime('%d.%m.%Y')} — {sunday.strftime('%d.%m.%Y')}):",
        reply_markup=kb,
    )


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

    text = format_lessons_for_day(day_lessons, iso_date)
    for chunk in split_message(text):
        await message.answer(chunk, parse_mode="HTML")


@router.message()
async def fallback(message: Message):
    await message.answer("Неизвестная команда. Попробуй /schedule")


async def main():
    bot = Bot(token=TOKEN)
    print("start")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())