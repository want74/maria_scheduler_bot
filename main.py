from os import getenv, path
import asyncio
import json
import re
import traceback
from datetime import date, timedelta, datetime
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputRichMessage,
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = getenv("BOT_TOKEN")
PROXY_URL = getenv("PROXY_URL", "socks5://kan:Parol_12@130.49.146.248:443")
USER_DATA_FILE = "user_groups.json"
SUBSCRIPTIONS_FILE = "subscriptions.json"

# ---------- Время рассылки: "HH:MM" ----------
DAILY_SEND_TIME = getenv("DAILY_SEND_TIME", "19:00")
try:
    _h, _m = DAILY_SEND_TIME.split(":")
    DAILY_SEND_HOUR, DAILY_SEND_MINUTE = int(_h), int(_m)
    assert 0 <= DAILY_SEND_HOUR < 24 and 0 <= DAILY_SEND_MINUTE < 60
except Exception:
    raise SystemExit(
        f"❌ DAILY_SEND_TIME должен быть в формате HH:MM (например, 21:47), "
        f"сейчас: {DAILY_SEND_TIME!r}"
    )

GROUPS_API = "https://ruz.guz.ru/api/dictionary/groups"
SCHEDULE_API = "https://ruz.guz.ru/api/schedule/group/{group}"

RU_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
FULL_DAYS = {
    "Пн": "Понедельник", "Вт": "Вторник", "Ср": "Среда",
    "Чт": "Четверг", "Пт": "Пятница", "Сб": "Суббота", "Вс": "Воскресенье",
}
DAY_BUTTON_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})\s*\((\w{2})\)$")

KIND_EDU_NAMES = {
    0: "Бакалавриат",
    1: "Магистратура",
    2: "Специалитет",
    3: "Аспирантура",
    5: "Подготовительное отделение",
}

# ---------- Проверка поддержки цветных inline-кнопок ----------
try:
    InlineKeyboardButton(text="_", callback_data="_", style="success")
    SUPPORTS_BTN_STYLE = True
except Exception:
    SUPPORTS_BTN_STYLE = False

# ---------- Глобальное состояние ----------
groups_cache: list[dict] = []
session: Optional[aiohttp.ClientSession] = None
user_week: dict[int, date] = {}
user_groups: dict[int, dict] = {}
subscriptions: dict[int, dict] = {}
bot_instance: Optional[Bot] = None

dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ---------- Хранилище выбранных групп ----------

def load_user_groups() -> dict[int, dict]:
    if not path.exists(USER_DATA_FILE):
        return {}
    try:
        with open(USER_DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except Exception:
        traceback.print_exc()
        return {}


def save_user_groups() -> None:
    with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in user_groups.items()},
                  f, ensure_ascii=False, indent=2)


# ---------- Хранилище подписок ----------

def load_subscriptions() -> dict[int, dict]:
    if not path.exists(SUBSCRIPTIONS_FILE):
        return {}
    try:
        with open(SUBSCRIPTIONS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except Exception:
        traceback.print_exc()
        return {}


def save_subscriptions() -> None:
    with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in subscriptions.items()},
                  f, ensure_ascii=False, indent=2)


# ---------- API ----------

async def fetch_groups() -> list:
    assert session is not None
    async with session.get(GROUPS_API) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_schedule(group_id: int, start: str, finish: str) -> list:
    assert session is not None
    url = SCHEDULE_API.format(group=group_id)
    params = {"start": start, "finish": finish, "lng": 1}
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


# ---------- Фильтрация справочника ----------

def _filter(year=None, kind_edu=None, faculty_oid=None, course=None,
            speciality=None, specialization=None) -> list[dict]:
    res = groups_cache
    if year is not None:
        res = [g for g in res if g.get("YearOfEducation") == year]
    if kind_edu is not None:
        res = [g for g in res if g.get("kindEducation") == kind_edu]
    if faculty_oid is not None:
        res = [g for g in res if g.get("facultyOid") == faculty_oid]
    if course is not None:
        res = [g for g in res if g.get("course") == course]
    if speciality is not None:
        res = [g for g in res if (g.get("speciality") or "") == speciality]
    if specialization is not None:
        res = [g for g in res if g.get("groupSpecialization_name") == specialization]
    return res


def _year_label(year: int) -> str:
    return f"{year}-{year + 1}"


def get_years() -> list[int]:
    return sorted({g["YearOfEducation"] for g in groups_cache}, reverse=True)


def get_kind_educations(year: int) -> list[int]:
    return sorted({
        g["kindEducation"]
        for g in groups_cache
        if g.get("YearOfEducation") == year
    })


def get_faculties(year: int, kind_edu: int) -> list[tuple[int, str]]:
    seen: dict[int, str] = {}
    for g in _filter(year=year, kind_edu=kind_edu):
        oid = g.get("facultyOid")
        if oid not in seen:
            seen[oid] = g.get("faculty") or f"Факультет {oid}"
    return sorted(seen.items(), key=lambda x: x[1])


def get_courses(year: int, kind_edu: int, faculty_oid: int) -> list[int]:
    return sorted({
        g["course"]
        for g in _filter(year=year, kind_edu=kind_edu, faculty_oid=faculty_oid)
    })


def get_specialities(year: int, kind_edu: int, faculty_oid: int,
                     course: int) -> list[str]:
    return sorted({
        (g.get("speciality") or "")
        for g in _filter(year=year, kind_edu=kind_edu, faculty_oid=faculty_oid,
                         course=course)
    })


def get_specializations(year: int, kind_edu: int, faculty_oid: int,
                        course: int, speciality: str) -> list[Optional[str]]:
    vals = {
        g.get("groupSpecialization_name")
        for g in _filter(year=year, kind_edu=kind_edu, faculty_oid=faculty_oid,
                         course=course, speciality=speciality)
    }
    return sorted(vals, key=lambda x: (x is None, x or ""))


def get_groups(year, kind_edu, faculty_oid, course,
               speciality, specialization) -> list[dict]:
    return sorted(
        _filter(year=year, kind_edu=kind_edu, faculty_oid=faculty_oid,
                course=course, speciality=speciality,
                specialization=specialization),
        key=lambda g: g["name"],
    )


def _kind_edu_label(code: int) -> str:
    return KIND_EDU_NAMES.get(code, f"Уровень {code}")


def _label_speciality(s: str) -> str:
    return s.strip() if s and s.strip() else "— без направления —"


def _label_specialization(s) -> str:
    return s if s else "— без профиля —"


# ---------- Визард выбора группы ----------

class Wizard(StatesGroup):
    year = State()
    kind_edu = State()
    faculty = State()
    course = State()
    speciality = State()
    specialization = State()
    group = State()


async def _send(target, text: str, reply_markup=None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(
                text, reply_markup=reply_markup, parse_mode="HTML"
            )
            return
        except Exception:
            pass
        await target.message.answer(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )
    elif isinstance(target, Message):
        await target.answer(text, reply_markup=reply_markup, parse_mode="HTML")


def _options_kb(options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=cb)]
                         for label, cb in options]
    )


def _btn(text: str, callback_data: str,
         style: Optional[str] = None) -> InlineKeyboardButton:
    if style and SUPPORTS_BTN_STYLE:
        return InlineKeyboardButton(text=text, callback_data=callback_data,
                                    style=style)
    return InlineKeyboardButton(text=text, callback_data=callback_data)


async def advance(target, state: FSMContext) -> None:
    data = await state.get_data()

    if "year" not in data:
        years = get_years()
        if not years:
            await _send(target, "❌ Справочник групп пуст.")
            return
        if len(years) == 1:
            await state.update_data(year=years[0])
            return await advance(target, state)
        await state.set_state(Wizard.year)
        return await _send(
            target, "📅 Выбери учебный год:",
            _options_kb([(_year_label(y), f"y:{y}") for y in years]),
        )

    year = data["year"]

    if "kind_edu" not in data:
        kes = get_kind_educations(year)
        if not kes:
            await _send(target, "❌ Нет уровней образования для выбранного года.")
            return
        if len(kes) == 1:
            await state.update_data(kind_edu=kes[0])
            return await advance(target, state)
        await state.set_state(Wizard.kind_edu)
        return await _send(
            target, "🎚 Выбери уровень образования:",
            _options_kb([(_kind_edu_label(k), f"ke:{k}") for k in kes]),
        )

    kind_edu = data["kind_edu"]

    if "faculty_oid" not in data:
        facs = get_faculties(year, kind_edu)
        if not facs:
            await _send(target, "❌ Нет факультетов для выбранных фильтров.")
            return
        if len(facs) == 1:
            await state.update_data(faculty_oid=facs[0][0])
            return await advance(target, state)
        await state.set_state(Wizard.faculty)
        return await _send(target, "🏛 Выбери факультет:",
                           _options_kb([(name, f"f:{oid}") for oid, name in facs]))

    faculty_oid = data["faculty_oid"]

    if "course" not in data:
        courses = get_courses(year, kind_edu, faculty_oid)
        if not courses:
            await _send(target, "❌ Нет курсов.")
            return
        if len(courses) == 1:
            await state.update_data(course=courses[0])
            return await advance(target, state)
        await state.set_state(Wizard.course)
        return await _send(target, "📚 Выбери курс:",
                           _options_kb([(f"{c} курс", f"c:{c}") for c in courses]))

    course = data["course"]

    if "speciality" not in data:
        specs = get_specialities(year, kind_edu, faculty_oid, course)
        if not specs:
            await _send(target, "❌ Нет направлений.")
            return
        if len(specs) == 1:
            await state.update_data(speciality=specs[0])
            return await advance(target, state)
        await state.set_state(Wizard.speciality)
        return await _send(target, "🎓 Выбери направление подготовки:",
                           _options_kb([(_label_speciality(s), f"s:{i}")
                                        for i, s in enumerate(specs)]))

    speciality = data["speciality"]

    if "specialization" not in data:
        sps = get_specializations(year, kind_edu, faculty_oid, course, speciality)
        if not sps:
            await _send(target, "❌ Нет профилей.")
            return
        if len(sps) == 1:
            await state.update_data(specialization=sps[0])
            return await advance(target, state)
        await state.set_state(Wizard.specialization)
        return await _send(target, "🧩 Выбери профиль / магистерскую программу:",
                           _options_kb([(_label_specialization(s), f"sp:{i}")
                                        for i, s in enumerate(sps)]))

    specialization = data["specialization"]

    grps = get_groups(year, kind_edu, faculty_oid, course,
                      speciality, specialization)
    if not grps:
        await _send(target, "❌ Группы не найдены.")
        return
    if len(grps) == 1:
        g = grps[0]
        await state.update_data(group_oid=g["groupOid"], group_name=g["name"],
                                group_guid=g["groupGUID"])
        return await finish(target, state)

    await state.set_state(Wizard.group)
    return await _send(target, "👥 Найдено несколько групп — выбери свою:",
                       _options_kb([(g["name"], f"g:{g['groupOid']}") for g in grps]))


async def finish(target, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = target.from_user.id

    user_groups[user_id] = {
        "group_oid": data["group_oid"],
        "group_name": data["group_name"],
        "group_guid": data["group_guid"],
    }
    save_user_groups()
    await state.clear()

    year = data.get("year")
    kind_edu = data.get("kind_edu")
    context_line = ""
    if year is not None and kind_edu is not None:
        context_line = f"\n✅ {_year_label(year)} · {_kind_edu_label(kind_edu)}"

    text = (
        f"✅ Группа сохранена: <b>{data['group_name']}</b>"
        f"{context_line}\n"
        "Нажми «📅 Расписание», чтобы смотреть пары."
    )

    if isinstance(target, CallbackQuery):
        await target.message.answer(
            text, reply_markup=main_menu_kb(), parse_mode="HTML"
        )
    else:
        await target.answer(
            text, reply_markup=main_menu_kb(), parse_mode="HTML"
        )


# ---------- Callback-хендлеры визарда ----------

@router.callback_query(Wizard.year, F.data.startswith("y:"))
async def on_year(cb: CallbackQuery, state: FSMContext):
    await state.update_data(year=int(cb.data.split(":")[1]))
    await cb.answer()
    await advance(cb, state)


@router.callback_query(Wizard.kind_edu, F.data.startswith("ke:"))
async def on_kind_edu(cb: CallbackQuery, state: FSMContext):
    await state.update_data(kind_edu=int(cb.data.split(":")[1]))
    await cb.answer()
    await advance(cb, state)


@router.callback_query(Wizard.faculty, F.data.startswith("f:"))
async def on_faculty(cb: CallbackQuery, state: FSMContext):
    await state.update_data(faculty_oid=int(cb.data.split(":")[1]))
    await cb.answer()
    await advance(cb, state)


@router.callback_query(Wizard.course, F.data.startswith("c:"))
async def on_course(cb: CallbackQuery, state: FSMContext):
    await state.update_data(course=int(cb.data.split(":")[1]))
    await cb.answer()
    await advance(cb, state)


@router.callback_query(Wizard.speciality, F.data.startswith("s:"))
async def on_speciality(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    d = await state.get_data()
    specs = get_specialities(d["year"], d["kind_edu"], d["faculty_oid"], d["course"])
    await state.update_data(speciality=specs[idx])
    await cb.answer()
    await advance(cb, state)


@router.callback_query(Wizard.specialization, F.data.startswith("sp:"))
async def on_specialization(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    d = await state.get_data()
    sps = get_specializations(d["year"], d["kind_edu"], d["faculty_oid"],
                              d["course"], d["speciality"])
    await state.update_data(specialization=sps[idx])
    await cb.answer()
    await advance(cb, state)


@router.callback_query(Wizard.group, F.data.startswith("g:"))
async def on_group(cb: CallbackQuery, state: FSMContext):
    group_oid = int(cb.data.split(":")[1])
    g = next((x for x in groups_cache if x["groupOid"] == group_oid), None)
    if not g:
        await cb.answer("Группа не найдена", show_alert=True)
        return
    await state.update_data(group_oid=g["groupOid"], group_name=g["name"],
                            group_guid=g["groupGUID"])
    await cb.answer()
    await finish(cb, state)


# ---------- Подписки ----------

def schedule_actions_kb(chat_id: int) -> InlineKeyboardMarkup:
    if chat_id in subscriptions:
        sub = subscriptions[chat_id]
        return InlineKeyboardMarkup(inline_keyboard=[[
            _btn(f"❌ Отписаться от {sub['group_name']}", "sub:off", style="danger")
        ]])
    group_info = user_groups.get(chat_id)
    label = (f"🔔 Подписаться на {group_info['group_name']}"
             if group_info else "🔔 Подписаться на рассылку")
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn(label, "sub:on", style="success")
    ]])


@router.callback_query(F.data == "sub:on")
async def on_subscribe(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    uid = cb.from_user.id
    if uid not in user_groups:
        await cb.answer("Сначала выбери группу через /start", show_alert=True)
        return
    g = user_groups[uid]
    subscriptions[chat_id] = {
        "group_oid": g["group_oid"],
        "group_name": g["group_name"],
        "group_guid": g["group_guid"],
    }
    save_subscriptions()
    await cb.answer(f"✅ Подписка оформлена на {g['group_name']}")
    try:
        await cb.message.edit_reply_markup(reply_markup=schedule_actions_kb(chat_id))
    except Exception:
        pass


@router.callback_query(F.data == "sub:off")
async def on_unsubscribe(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if chat_id in subscriptions:
        del subscriptions[chat_id]
        save_subscriptions()
    await cb.answer("❌ Подписка отменена")
    try:
        await cb.message.edit_reply_markup(reply_markup=schedule_actions_kb(chat_id))
    except Exception:
        pass


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    uid = message.from_user.id
    if uid not in user_groups:
        await message.answer("Сначала выбери группу: /start")
        return
    g = user_groups[uid]
    subscriptions[message.chat.id] = {
        "group_oid": g["group_oid"],
        "group_name": g["group_name"],
        "group_guid": g["group_guid"],
    }
    save_subscriptions()
    await message.answer(
        f"✅ Ты подписан на ежедневную рассылку расписания "
        f"для <b>{g['group_name']}</b>.\n"
        f"Расписание на завтра будет приходить в "
        f"{DAILY_SEND_HOUR:02d}:{DAILY_SEND_MINUTE:02d}.",
        parse_mode="HTML",
    )


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    if message.chat.id in subscriptions:
        del subscriptions[message.chat.id]
        save_subscriptions()
        await message.answer("❌ Подписка отменена.")
    else:
        await message.answer("Ты и так не подписан.")


# ---------- Меню и расписание ----------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Расписание")],
            [KeyboardButton(text="🔁 Сменить группу")],
        ],
        resize_keyboard=True,
    )


def current_monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def get_user_monday(chat_id: int) -> date:
    return user_week.get(chat_id, current_monday())


def build_days_keyboard(monday: date) -> ReplyKeyboardMarkup:
    keyboard = []
    row = []
    for i in range(6):
        d = monday + timedelta(days=i)
        row.append(KeyboardButton(text=f"{d.strftime('%d.%m.%Y')} ({RU_DAYS[i]})"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([KeyboardButton(text="<<"), KeyboardButton(text=">>")])
    keyboard.append([KeyboardButton(text="🏠 В меню")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


async def show_week(message: Message, monday: date) -> None:
    sunday = monday + timedelta(days=6)
    await message.answer(
        f"Неделя {monday.strftime('%d.%m.%Y')} — {sunday.strftime('%d.%m.%Y')}:",
        reply_markup=build_days_keyboard(monday),
    )


# ---------- Форматирование ----------

def _pick_group_label(lessons: list) -> str:
    for l in lessons:
        g = (l.get("subGroup") or l.get("stream")
             or (l.get("listGroups") or [{}])[0].get("group"))
        if g:
            return g
    return ""


def format_lessons_for_day(lessons: list, iso_date: str) -> str:
    yyyy, mm, dd = iso_date.split("-")
    pretty = f"{dd}.{mm}.{yyyy}"
    if not lessons:
        return f"<p><b>📅 Занятий нет</b> — {pretty}</p>"

    short = lessons[0].get("dayOfWeekString", "")
    full = FULL_DAYS.get(short, short)
    grp = _pick_group_label(lessons)

    html = f"<h3>📅 {full} ({pretty})</h3>"
    if grp:
        html += f"<p>Группа {grp}</p>"
    html += ("<table border='1'><tr>"
             "<th align='left'>Время</th>"
             "<th align='left'>Занятие</th>"
             "<th align='left'>Информация</th></tr>")
    for l in sorted(lessons, key=lambda x: x["beginLesson"]):
        html += (
            "<tr>"
            f"<td>{l['beginLesson']}–{l['endLesson']}</td>"
            f"<td><b>{l['discipline']}</b><br/>{l['auditorium']} ({l['building']})</td>"
            f"<td>{l['kindOfWork']}, {l['lecturer']}</td>"
            "</tr>"
        )
    html += "</table>"
    return html


def format_lessons_for_day_plain(lessons: list, iso_date: str) -> str:
    yyyy, mm, dd = iso_date.split("-")
    pretty = f"{dd}.{mm}.{yyyy}"
    if not lessons:
        return f"📅 <b>Занятий нет</b> — {pretty}"

    short = lessons[0].get("dayOfWeekString", "")
    full = FULL_DAYS.get(short, short)
    grp = _pick_group_label(lessons)

    lines = [f"📅 <b>{full}</b> ({pretty})"]
    if grp:
        lines.append(f"Группа: <b>{grp}</b>")
    lines.append("")

    for l in sorted(lessons, key=lambda x: x["beginLesson"]):
        lines.append(
            f"🕐 <b>{l['beginLesson']}–{l['endLesson']}</b>\n"
            f"📚 {l['discipline']}\n"
            f"📍 {l['auditorium']} ({l['building']})\n"
            f"👤 {l['kindOfWork']}, {l['lecturer']}"
        )
        lines.append("")

    return "\n".join(lines).rstrip()


def split_message(text: str, limit: int = 30000) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)]


# ---------- Фоновая рассылка ----------

async def send_rich(bot: Bot, chat_id: int, html: str, reply_markup=None) -> bool:
    for name in ("send_rich", "send_rich_message"):
        method = getattr(bot, name, None)
        if callable(method):
            try:
                await method(
                    chat_id=chat_id,
                    rich_message=InputRichMessage(html=html),
                    reply_markup=reply_markup,
                )
                return True
            except Exception:
                traceback.print_exc()
    return False


async def send_daily_to_all(bot: Bot) -> None:
    if not subscriptions:
        print("[scheduler] подписок нет")
        return

    tomorrow = date.today() + timedelta(days=1)
    api_date = tomorrow.strftime("%Y.%m.%d")
    iso_date = tomorrow.strftime("%Y-%m-%d")

    print(f"[scheduler] рассылка на {iso_date} ({api_date}), подписок: {len(subscriptions)}")

    for chat_id, sub in list(subscriptions.items()):
        try:
            print(f"[scheduler] → chat {chat_id} | group {sub['group_oid']} ({sub['group_name']})")
            lessons = await fetch_schedule(sub["group_oid"], api_date, api_date)
            day_lessons = [l for l in lessons if l.get("date") == iso_date]
            print(f"[scheduler]   API вернул {len(lessons)} занятий, из них на завтра: {len(day_lessons)}")

            if not day_lessons:
                print("[scheduler]   занятий нет — пропускаю (сообщение не отправляется)")
                continue

            kb = schedule_actions_kb(chat_id)
            rich = format_lessons_for_day(day_lessons, iso_date)
            plain = format_lessons_for_day_plain(day_lessons, iso_date)

            sent = await send_rich(bot, chat_id, rich, reply_markup=kb)
            if not sent:
                print("[scheduler]   rich не сработал, отправляю plain HTML")
                await bot.send_message(chat_id, plain, parse_mode="HTML",
                                       reply_markup=kb)
            print(f"[scheduler]   ✅ отправлено в chat {chat_id}")
        except Exception as e:
            print(f"[scheduler]   ❌ ОШИБКА для chat {chat_id}: {type(e).__name__}: {e}")
            traceback.print_exc()


async def daily_loop(bot: Bot) -> None:
    while True:
        try:
            now = datetime.now()
            target = now.replace(
                hour=DAILY_SEND_HOUR,
                minute=DAILY_SEND_MINUTE,
                second=0,
                microsecond=0,
            )
            if target <= now:
                target += timedelta(days=1)
            wait_s = (target - now).total_seconds()
            print(f"[scheduler] следующая рассылка: {target} (через {wait_s / 3600:.1f} ч)")
            await asyncio.sleep(wait_s)
            await send_daily_to_all(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            await asyncio.sleep(60)


# ---------- Хендлеры меню ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if uid in user_groups:
        g = user_groups[uid]
        await message.answer(
            f"👋 Привет! Твоя группа: <b>{g['group_name']}</b>",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
    else:
        await message.answer("👋 Привет! Давай выберем твою группу.")
        await advance(message, state)


@router.message(Command("change_group"))
@router.message(F.text == "🔁 Сменить группу")
async def cmd_change_group(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔁 Выбери группу заново.")
    await advance(message, state)


@router.message(Command("schedule"))
@router.message(F.text == "📅 Расписание")
async def cmd_schedule(message: Message):
    if message.from_user.id not in user_groups:
        await message.answer("Сначала выбери группу: /start")
        return
    monday = current_monday()
    user_week[message.chat.id] = monday
    await show_week(message, monday)


@router.message(F.text == "🏠 В меню")
async def on_home(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(F.text == "<<")
async def on_prev_week(message: Message):
    monday = get_user_monday(message.chat.id) - timedelta(days=7)
    user_week[message.chat.id] = monday
    await show_week(message, monday)


@router.message(F.text == ">>")
async def on_next_week(message: Message):
    monday = get_user_monday(message.chat.id) + timedelta(days=7)
    user_week[message.chat.id] = monday
    await show_week(message, monday)


@router.message(lambda m: m.text and DAY_BUTTON_RE.match(m.text))
async def on_day_press(message: Message):
    uid = message.from_user.id
    if uid not in user_groups:
        await message.answer("Сначала выбери группу: /start")
        return

    group_oid = user_groups[uid]["group_oid"]
    m = DAY_BUTTON_RE.match(message.text)
    dd, mm, yyyy, _ = m.groups()
    api_date = f"{yyyy}.{mm}.{dd}"
    iso_date = f"{yyyy}-{mm}-{dd}"

    await message.answer(f"⏳ Загружаю {dd}.{mm}.{yyyy}…")

    try:
        lessons = await fetch_schedule(group_oid, api_date, api_date)
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
        await message.answer_rich(rich_message=InputRichMessage(html=chunk))

    if message.chat.id in subscriptions:
        hint = "🔔 Рассылка расписания на завтра включена."
    else:
        hint = "🔔 Хочешь получать расписание на завтра каждый день?"
    await message.answer(
        hint,
        reply_markup=schedule_actions_kb(message.chat.id),
        parse_mode="HTML",
    )


@router.message(Command("reload"))
async def cmd_reload(message: Message):
    global groups_cache
    try:
        groups_cache = await fetch_groups()
        await message.answer(f"🔄 Справочник обновлён: {len(groups_cache)} групп.")
    except Exception as e:
        await message.answer(f"❌ Не удалось обновить: {e}")


@router.message(Command("test_daily"))
async def cmd_test_daily(message: Message):
    if bot_instance is None:
        await message.answer("Бот не инициализирован.")
        return
    await message.answer("⏳ Запускаю тестовую рассылку…")
    await send_daily_to_all(bot_instance)
    await message.answer("✅ Тест завершён. Смотри консоль.")


@router.message()
async def fallback(message: Message):
    await message.answer("Неизвестная команда. /start или /schedule")


# ---------- Точка входа ----------

async def main():
    global session, groups_cache, user_groups, subscriptions, bot_instance

    connector = ProxyConnector.from_url(PROXY_URL)
    session = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=20),
    )
    try:
        groups_cache = await fetch_groups()
        print(f"Загружено групп: {len(groups_cache)}")
    except Exception as e:
        print(f"Не удалось загрузить справочник групп: {e}")
        traceback.print_exc()

    user_groups = load_user_groups()
    subscriptions = load_subscriptions()
    print(f"Загружено подписок: {len(subscriptions)}")

    bot = Bot(token=TOKEN)
    bot_instance = bot
    print("start")

    # ⚠️ Убираем возможный webhook, чтобы polling не падал с Conflict
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("[init] webhook удалён (если был)")
    except Exception as e:
        print(f"[init] не удалось удалить webhook: {type(e).__name__}: {e}")

    scheduler_task = asyncio.create_task(daily_loop(bot))
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        await session.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())