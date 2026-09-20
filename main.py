from os import getenv
import asyncio
from datetime import date, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()
TOKEN = getenv("BOT_TOKEN")

GROUP_ID = 99
API_URL = "https://ruz.guz.ru/api/schedule/group/{group}"

dp = Dispatcher()
router = Router()
dp.include_router(router)


async def fetch_schedule(group_id: int, start: str, finish: str) -> list:
    """Загружает расписание с API РУЗ."""
    url = API_URL.format(group=group_id)
    params = {"start": start, "finish": finish, "lng": 1}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=15) as resp:
            resp.raise_for_status()
            return await resp.json()


def format_lessons(lessons: list) -> str:
    """Форматирует список занятий в читаемый текст."""
    if not lessons:
        return "На эту неделю занятий нет."

    # группируем по дате
    by_date: dict[str, list] = {}
    for l in lessons:
        by_date.setdefault(l["date"], []).append(l)

    lines = []
    for d in sorted(by_date):
        day_name = by_date[d][0].get("dayOfWeekString", "")
        lines.append(f"\n📅 {d} ({day_name})")
        for l in sorted(by_date[d], key=lambda x: x["beginLesson"]):
            lines.append(
                f"  • {l['beginLesson']}–{l['endLesson']} | {l['discipline']}\n"
                f"    {l['kindOfWork']}, ауд. {l['auditorium']} ({l['building']})\n"
                f"    {l['lecturer']}"
            )
    return "\n".join(lines)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Режет длинный текст на части (лимит Telegram — 4096)."""
    return [text[i:i + limit] for i in range(0, len(text), limit)]


@router.message(Command("schedule"))
async def cmd_schedule(message):
    # текущая неделя: понедельник — воскресенье
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    start = monday.strftime("%Y.%m.%d")
    finish = sunday.strftime("%Y.%m.%d")

    await message.answer(f"⏳ Загружаю расписание {start} — {finish}…")

    try:
        lessons = await fetch_schedule(GROUP_ID, start, finish)
    except aiohttp.ClientError as e:
        await message.answer(f"❌ Ошибка сети: {e}")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    text = format_lessons(lessons)
    for chunk in split_message(text):
        await message.answer(chunk)


@router.message()
async def hello(message):
    await message.answer("Hello")


async def main():
    bot = Bot(token=TOKEN)
    print("start")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())