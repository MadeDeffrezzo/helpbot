import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta
from html import escape as html_escape

import pytz
from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= НАСТРОЙКА И БЕЗОПАСНОСТЬ =================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it to environment variables or .env")

DB_NAME = os.getenv("DB_NAME", "pill_reminder.db")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# Список часовых поясов России
RU_TIMEZONES = {
    "zone_kaliningrad": ("Калининград (МСК-1 / UTC+2)", "Europe/Kaliningrad"),
    "zone_moscow": ("Москва (МСК / UTC+3)", "Europe/Moscow"),
    "zone_samara": ("Самара (МСК+1 / UTC+4)", "Europe/Samara"),
    "zone_ekaterinburg": ("Екатеринбург (МСК+2 / UTC+5)", "Asia/Yekaterinburg"),
    "zone_omsk": ("Омск (МСК+3 / UTC+6)", "Asia/Omsk"),
    "zone_krasnoyarsk": ("Красноярск (МСК+4 / UTC+7)", "Asia/Krasnoyarsk"),
    "zone_irkutsk": ("Иркутск (МСК+5 / UTC+8)", "Asia/Irkutsk"),
    "zone_yakutsk": ("Якутск (МСК+6 / UTC+9)", "Asia/Yakutsk"),
    "zone_vladivostok": ("Владивосток (МСК+7 / UTC+10)", "Asia/Vladivostok"),
    "zone_magadan": ("Магадан (МСК+8 / UTC+11)", "Asia/Magadan"),
    "zone_kamchatka": ("Камчатка (МСК+9 / UTC+12)", "Asia/Anadyr"),
}


# ================= БАЗА ДАННЫХ =================
CONDITION_OPTIONS = {
    "none": "не указано",
    "before": "до еды",
    "during": "во время еды",
    "after": "после еды",
    "empty": "натощак",
    "night": "перед сном",
}

SCHEDULE_MODES = {
    "slots": "Точное время",
    "interval": "Через равные интервалы",
}

PENDING_MATH_QUIZ = {}


def get_condition_label(value):
    if value is None:
        return "не указано"
    if value in CONDITION_OPTIONS:
        return CONDITION_OPTIONS[value]
    for key, label in CONDITION_OPTIONS.items():
        if value == label:
            return label
    return "не указано"


def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT
            )"""
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                pill_name TEXT,
                time_str TEXT,
                condition TEXT DEFAULT 'none',
                schedule_mode TEXT DEFAULT 'slots',
                times_per_day INTEGER DEFAULT 1,
                interval_hours REAL DEFAULT 0,
                first_time TEXT DEFAULT '',
                time_slots TEXT DEFAULT ''
            )"""
        )
        conn.commit()

        cursor.execute("PRAGMA table_info(reminders)")
        columns = {row[1] for row in cursor.fetchall()}
        type_map = {
            "condition": "TEXT DEFAULT 'none'",
            "schedule_mode": "TEXT DEFAULT 'slots'",
            "times_per_day": "INTEGER DEFAULT 1",
            "interval_hours": "REAL DEFAULT 0",
            "first_time": "TEXT DEFAULT ''",
            "time_slots": "TEXT DEFAULT ''",
        }
        for col_name, column_def in type_map.items():
            if col_name not in columns:
                cursor.execute(f"ALTER TABLE reminders ADD COLUMN {col_name} {column_def}")
        conn.commit()


def set_user_tz(user_id, tz_name):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO users (user_id, timezone) VALUES (?, ?)",
            (user_id, tz_name),
        )
        conn.commit()


def get_user_tz(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None


def get_tz_label(tz_name):
    for key, (label, value) in RU_TIMEZONES.items():
        if value == tz_name:
            return label
    return tz_name or "UTC"


def add_reminder(
    user_id,
    pill_name,
    time_str,
    condition="none",
    schedule_mode="slots",
    times_per_day=1,
    interval_hours=0,
    first_time="",
    time_slots="",
):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        normalized = condition if condition in CONDITION_OPTIONS else "none"
        cursor.execute(
            """
            INSERT INTO reminders (
                user_id, pill_name, time_str, condition,
                schedule_mode, times_per_day, interval_hours,
                first_time, time_slots
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                pill_name,
                time_str,
                normalized,
                schedule_mode,
                times_per_day,
                interval_hours,
                first_time,
                time_slots,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def get_user_reminders(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, pill_name, time_str, condition FROM reminders WHERE user_id = ? ORDER BY time_str ASC",
            (user_id,),
        )
        return cursor.fetchall()


def get_reminder_by_id(reminder_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, pill_name, time_str, condition FROM reminders WHERE id = ?", (reminder_id,))
        row = cursor.fetchone()
        return row if row else None


def update_reminder(
    reminder_id,
    pill_name=None,
    time_str=None,
    condition=None,
    schedule_mode=None,
    times_per_day=None,
    interval_hours=None,
    first_time=None,
    time_slots=None,
):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        values = []
        if pill_name is not None:
            values.append(("pill_name", pill_name))
        if time_str is not None:
            values.append(("time_str", time_str))
        if condition is not None:
            normalized = condition if condition in CONDITION_OPTIONS else "none"
            values.append(("condition", normalized))
        if schedule_mode is not None:
            values.append(("schedule_mode", schedule_mode))
        if times_per_day is not None:
            values.append(("times_per_day", times_per_day))
        if interval_hours is not None:
            values.append(("interval_hours", interval_hours))
        if first_time is not None:
            values.append(("first_time", first_time))
        if time_slots is not None:
            values.append(("time_slots", time_slots))
        if not values:
            return False

        assignments = ", ".join(f"{field} = ?" for field, _ in values)
        params = [value for _, value in values]
        params.append(reminder_id)
        cursor.execute(f"UPDATE reminders SET {assignments} WHERE id = ?", params)
        conn.commit()
        return True


def delete_reminder_db(reminder_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()

    for job_id in list(scheduler.get_jobs()) if hasattr(scheduler, "get_jobs") else []:
        job_name = job_id.id if hasattr(job_id, "id") else str(job_id)
        if job_name.startswith(f"rem_{reminder_id}") or job_name.startswith(f"delay_{reminder_id}") or job_name.startswith(f"quiz_{reminder_id}"):
            scheduler.remove_job(job_name)


# ================= СОСТОЯНИЯ (FSM) =================
class Form(StatesGroup):
    waiting_for_pill_name = State()
    waiting_for_time = State()
    waiting_for_schedule_mode = State()
    waiting_for_schedule_count = State()
    waiting_for_slot_times = State()
    waiting_for_interval_value = State()
    waiting_for_condition = State()
    waiting_for_new_name = State()
    waiting_for_new_time = State()
    waiting_for_new_condition = State()
    waiting_for_calc_interval = State()
    waiting_for_calc_amount = State()
    waiting_for_calc_start = State()


# ================= КЛАВИАТУРЫ =================
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💊 Добавить лекарство")],
            [KeyboardButton(text="📋 Мои лекарства"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="🧮 Калькулятор дозировки")],
        ],
        resize_keyboard=True,
    )


def get_condition_keyboard(prefix="add"):
    rows = []
    for key, label in CONDITION_OPTIONS.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f"cond_{prefix}_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_schedule_mode_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🕐 Точное время", callback_data="schedule_mode_slots")],
            [InlineKeyboardButton(text="⏱ Интервал в часах", callback_data="schedule_mode_interval")],
        ]
    )


def get_edit_keyboard(reminder_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_name_{reminder_id}")],
            [InlineKeyboardButton(text="⏰ Время", callback_data=f"edit_time_{reminder_id}")],
            [InlineKeyboardButton(text="🕒 Расписание", callback_data=f"edit_schedule_{reminder_id}")],
            [InlineKeyboardButton(text="🥗 Условие", callback_data=f"edit_condition_{reminder_id}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{reminder_id}")],
        ]
    )


def get_tz_keyboard():
    buttons = []
    for key, (label, _) in RU_TIMEZONES.items():
        buttons.append([InlineKeyboardButton(text=label, callback_data=key)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ================= ПЛАНИРОВАНИЕ ЗАДАЧ =================

def normalize_schedule_reminder(reminder_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, pill_name, time_str, condition, schedule_mode, times_per_day, interval_hours, first_time, time_slots FROM reminders WHERE id = ?",
            (reminder_id,),
        )
        row = cursor.fetchone()
    if not row:
        return []

    user_id, pill_name, time_str, condition, schedule_mode, times_per_day, interval_hours, first_time, time_slots = row
    times = []
    mode = (schedule_mode or "slots").strip() or "slots"
    count = int(times_per_day or 1)
    if mode == "interval":
        start_time = first_time or time_str
        interval = float(interval_hours or 0)
        if start_time and interval > 0:
            start_dt = datetime.strptime(start_time, "%H:%M")
            for i in range(count):
                times.append((start_dt + timedelta(hours=interval * i)).strftime("%H:%M"))
        elif time_str:
            times.append(time_str)
    else:
        raw_slots = time_slots or time_str
        for item in str(raw_slots).split(","):
            value = item.strip()
            if value:
                try:
                    datetime.strptime(value, "%H:%M")
                    times.append(value)
                except ValueError:
                    continue
        if not times and time_str:
            times.append(time_str)
    return times


async def send_smart_reminder(user_id: int, pill_name: str, reminder_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, condition FROM reminders WHERE id = ?", (reminder_id,))
        row = cursor.fetchone()
        if not row:
            return
        _, condition = row

    condition_text = get_condition_label(condition)
    a = random.randint(1, 5)
    b = random.randint(1, 5)
    answer = a + b
    PENDING_MATH_QUIZ[reminder_id] = {"user_id": user_id, "pill_name": pill_name, "answer": answer, "solved": False}

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data=f"done_{reminder_id}")],
            [InlineKeyboardButton(text="⏰ Напомнить через 5 минут", callback_data=f"delay_{reminder_id}")],
        ]
    )
    try:
        await bot.send_message(
            user_id,
            f"🔔 Время принять лекарство: <b>{html_escape(pill_name)}</b>\n📌 Условие: <b>{html_escape(condition_text)}</b>\n\n🧠 Быстрая проверка: <b>{a} + {b}</b> = ?\nНапишите ответ цифрой, либо нажмите кнопку ниже.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

    if scheduler.get_job(f"quiz_{reminder_id}"):
        scheduler.remove_job(f"quiz_{reminder_id}")
    scheduler.add_job(
        send_smart_reminder,
        "interval",
        minutes=1,
        args=[user_id, pill_name, reminder_id],
        id=f"quiz_{reminder_id}",
        replace_existing=True,
    )


async def send_pill_reminder(user_id: int, pill_name: str, reminder_id: int):
    await send_smart_reminder(user_id, pill_name, reminder_id)


def schedule_reminder(user_id, pill_name, time_str, reminder_id, tz_name):
    try:
        if not tz_name:
            logging.warning(f"Не удалось запланировать напоминание {reminder_id}: не задан часовой пояс пользователя {user_id}")
            return

        schedule_rows = normalize_schedule_reminder(reminder_id)
        if not schedule_rows:
            schedule_rows = [time_str]

        user_tz = pytz.timezone(tz_name)
        now_user = datetime.now(user_tz)

        for idx, slot in enumerate(schedule_rows):
            target_time = datetime.strptime(slot, "%H:%M").time()
            job_datetime = datetime.combine(now_user.date(), target_time)
            job_datetime = user_tz.localize(job_datetime)

            if job_datetime < now_user:
                job_datetime += timedelta(days=1)

            job_id = f"rem_{reminder_id}_{idx}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)

            scheduler.add_job(
                send_pill_reminder,
                "cron",
                hour=job_datetime.hour,
                minute=job_datetime.minute,
                second=0,
                timezone=user_tz,
                args=[user_id, pill_name, reminder_id],
                id=job_id,
                replace_existing=True,
            )
            logging.info(f"Добавлена задача {job_id} на {slot} ({tz_name})")
    except Exception as e:
        logging.error(f"Ошибка калибровки времени задачи: {e}")


def restart_all_reminders():
    rows = []
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT r.id, r.user_id, r.pill_name, r.time_str, u.timezone 
            FROM reminders r 
            JOIN users u ON r.user_id = u.user_id
        """
        )
        rows = cursor.fetchall()

    try:
        scheduler.remove_all_jobs()
    except Exception as e:
        logging.warning(f"Не удалось удалить старые job'ы перед восстановлением: {e}")

    for row in rows:
        rem_id, u_id, name, t_str, tz = row
        schedule_reminder(u_id, name, t_str, rem_id, tz)
    logging.info(f"Успешно восстановлено задач из базы: {len(rows)}")


# ================= ОБРАБОТЧИКИ КОМАНД И КНОПОК =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    init_db()
    await message.answer(
        "Привет! Я надежный бот-напоминалка о приеме лекарств. 💊\n\n"
        "Пожалуйста, выберите ваш **часовой пояс** для точной отправки уведомлений:",
        reply_markup=get_tz_keyboard(),
    )


@dp.callback_query(F.data.startswith("zone_"))
async def select_timezone(callback: CallbackQuery):
    tz_code = callback.data
    label, tz_name = RU_TIMEZONES[tz_code]
    set_user_tz(callback.from_user.id, tz_name)

    await callback.message.edit_text(f"✅ Успешно установлен часовой пояс:\n<b>{label}</b>", parse_mode="HTML")
    await callback.message.answer(
        "Вы можете добавлять лекарства и настраивать график через меню ниже.", reply_markup=get_main_menu()
    )
    await callback.answer()


@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message):
    await message.answer("Выберите ваш актуальный часовой пояс России:", reply_markup=get_tz_keyboard())


@dp.message(F.text == "💊 Добавить лекарство")
async def add_pill_start(message: Message, state: FSMContext):
    tz = get_user_tz(message.from_user.id)
    if not tz:
        await message.answer("⚠️ Сначала укажите ваш часовой пояс в разделе ⚙️ Настройки")
        return
    await state.set_state(Form.waiting_for_pill_name)
    await message.answer("Введите точное название лекарства (например: Ибупрофен):")


@dp.message(Form.waiting_for_pill_name)
async def add_pill_name(message: Message, state: FSMContext):
    pill_name = message.text.strip()
    if not pill_name:
        await message.answer("❌ Название лекарства не может быть пустым. Введите корректное название.")
        return

    await state.update_data(pill_name=pill_name)
    await state.set_state(Form.waiting_for_time)
    await message.answer("Укажите время приема в формате **ЧЧ:ММ** (например: 08:00 или 22:45):")


@dp.message(Form.waiting_for_time)
async def add_pill_time(message: Message, state: FSMContext):
    time_str = (message.text or "").strip()
    if not time_str:
        await message.answer("❌ Время не введено. Попробуйте ещё раз в формате ЧЧ:ММ.")
        return

    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await message.answer("❌ Некорректный формат времени! Попробуйте еще раз. Пример: 07:15 или 20:00")
        return

    try:
        user_data = await state.get_data()
        pill_name = str(user_data.get("pill_name", "")).strip()
        if not pill_name:
            await message.answer("❌ Название лекарства не распознано. Попробуйте добавить лекарство заново.")
            await state.clear()
            return

        await state.update_data(time_str=time_str)
        await state.set_state(Form.waiting_for_schedule_mode)
        await message.answer("Как хотели бы задавать приемы?", reply_markup=get_schedule_mode_keyboard())
    except Exception:
        logging.exception("Ошибка при подготовке условия для напоминания")
        await state.clear()
        await message.answer("⚠️ Не удалось подготовить напоминание. Попробуйте ещё раз с начала.")


@dp.callback_query(F.data.startswith("schedule_mode_"))
async def set_schedule_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.replace("schedule_mode_", "")
    await state.update_data(schedule_mode=mode)
    await state.set_state(Form.waiting_for_schedule_count)
    await callback.message.edit_text("Сколько раз в день принимать? Введите число от 1 до 6:")
    await callback.answer()


@dp.message(Form.waiting_for_schedule_count)
async def set_schedule_count(message: Message, state: FSMContext):
    try:
        count = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Введите число от 1 до 6.")
        return
    if count < 1 or count > 6:
        await message.answer("❌ Введите число от 1 до 6.")
        return

    data = await state.get_data()
    mode = data.get("schedule_mode", "slots")
    await state.update_data(times_per_day=count)
    if mode == "slots":
        await state.set_state(Form.waiting_for_slot_times)
        await message.answer("Введите точные времена через запятую, например: 08:00, 13:00, 19:00")
    else:
        await state.set_state(Form.waiting_for_interval_value)
        await message.answer("Введите первый прием и шаг в часах через пробел, например: 08:00 6")


@dp.message(Form.waiting_for_slot_times)
async def set_slot_times(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    slots = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            datetime.strptime(value, "%H:%M")
            slots.append(value)
        except ValueError:
            await message.answer("❌ В одном из времен ошибка. Используйте формат ЧЧ:ММ, например: 08:00, 13:30")
            return

    data = await state.get_data()
    count = int(data.get("times_per_day", 1))
    slots = slots[:count]
    if len(slots) < count:
        await message.answer(f"❌ Нужно указать ровно {count} времени(ен) через запятую.")
        return

    await state.update_data(time_slots=", ".join(slots), schedule_mode="slots")
    edit_id = data.get("edit_reminder_id")
    if edit_id is not None:
        update_reminder(edit_id, schedule_mode="slots", times_per_day=count, time_slots=", ".join(slots))
        reminder = get_reminder_by_id(edit_id)
        if reminder:
            user_id, pill_name, _, _ = reminder
            schedule_reminder(user_id, pill_name, reminder[2], edit_id, get_user_tz(user_id))
        await state.clear()
        await message.answer("✅ Расписание обновлено.", reply_markup=get_main_menu())
        return

    await state.set_state(Form.waiting_for_condition)
    await message.answer("Выберите, когда принимать лекарство:", reply_markup=get_condition_keyboard("add"))


@dp.message(Form.waiting_for_interval_value)
async def set_interval_value(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        parts = raw.split()
        if len(parts) != 2:
            raise ValueError
        first_time, interval_hours = parts
        datetime.strptime(first_time, "%H:%M")
        interval = float(interval_hours)
        if interval <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: 08:00 6")
        return

    data = await state.get_data()
    count = int(data.get("times_per_day", 1))
    await state.update_data(first_time=first_time, interval_hours=interval, times_per_day=count, schedule_mode="interval")
    edit_id = data.get("edit_reminder_id")
    if edit_id is not None:
        update_reminder(edit_id, schedule_mode="interval", times_per_day=count, first_time=first_time, interval_hours=interval)
        reminder = get_reminder_by_id(edit_id)
        if reminder:
            user_id, pill_name, _, _ = reminder
            schedule_reminder(user_id, pill_name, reminder[2], edit_id, get_user_tz(user_id))
        await state.clear()
        await message.answer("✅ Расписание обновлено.", reply_markup=get_main_menu())
        return

    await state.set_state(Form.waiting_for_condition)
    await message.answer("Выберите, когда принимать лекарство:", reply_markup=get_condition_keyboard("add"))


@dp.callback_query(F.data.startswith("cond_"))
async def handle_condition_choice(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("⚠️ Не удалось обработать условие.", show_alert=True)
        return

    action = parts[1]
    condition_key = parts[2]
    condition_label = get_condition_label(condition_key)

    try:
        if action == "add":
            user_data = await state.get_data()
            user_id = callback.from_user.id
            tz_name = get_user_tz(user_id)
            pill_name = str(user_data.get("pill_name", "")).strip()
            time_str = str(user_data.get("time_str", "")).strip()

            if not pill_name or not time_str:
                await callback.message.edit_text("⚠️ Данные напоминания потеряны. Попробуйте добавить лекарство заново.")
                await callback.answer()
                return

            rem_id = add_reminder(
                user_id,
                pill_name,
                time_str,
                condition=condition_key,
                schedule_mode=user_data.get("schedule_mode", "slots"),
                times_per_day=int(user_data.get("times_per_day", 1) or 1),
                interval_hours=float(user_data.get("interval_hours", 0) or 0),
                first_time=str(user_data.get("first_time", "") or ""),
                time_slots=str(user_data.get("time_slots", "") or ""),
            )
            schedule_reminder(user_id, pill_name, time_str, rem_id, tz_name)

            await state.clear()
            tz_label = get_tz_label(tz_name)
            summary = (
                "✅ Напоминание добавлено\n"
                f"💊 <b>{html_escape(pill_name)}</b>\n"
                f"⏰ <b>{time_str}</b>\n"
                f"🥗 <b>{html_escape(condition_label)}</b>\n"
                f"🕒 {tz_label}"
            )
            await callback.answer()
            await callback.message.edit_text(summary, parse_mode="HTML")
            await callback.message.answer("Главное меню", reply_markup=get_main_menu())
            return

        if action == "edit":
            edit_id = (await state.get_data()).get("edit_reminder_id")
            if edit_id is None:
                await callback.message.edit_text("⚠️ Нет активного редактирования.")
                await callback.answer()
                return

            update_reminder(edit_id, condition=condition_key)
            reminder = get_reminder_by_id(edit_id)
            if reminder:
                user_id, pill_name, time_str, _ = reminder
                tz_name = get_user_tz(user_id)
                schedule_reminder(user_id, pill_name, time_str, edit_id, tz_name)

            await state.clear()
            await callback.answer()
            await callback.message.edit_text(
                f"✅ Условие обновлено: <b>{html_escape(condition_label)}</b>",
                parse_mode="HTML",
            )
            await callback.message.answer("Главное меню", reply_markup=get_main_menu())
            return

        await callback.answer("⚠️ Неверный тип условия.", show_alert=True)
    except Exception:
        logging.exception("Ошибка при обработке выбора условия")
        await callback.answer("⚠️ Ошибка при сохранении условия.", show_alert=True)


@dp.message(F.text == "📋 Мои лекарства")
async def list_reminders(message: Message):
    reminders = get_user_reminders(message.from_user.id)
    if not reminders:
        await message.answer("У вас пока нет активных напоминаний. Нажмите «💊 Добавить лекарство», чтобы создать первое.")
        return

    tz_name = get_user_tz(message.from_user.id)
    timezone_label = get_tz_label(tz_name)
    lines = [f"📋 <b>Ваш текущий график приема лекарств</b>", f"⏱️ Часовой пояс: {timezone_label}"]

    for rem_id, pill_name, time_str, condition in reminders:
        condition_label = get_condition_label(condition)
        lines.append(f"\n💊 <b>{html_escape(pill_name)}</b> — ⏰ {time_str} — 🥗 {html_escape(condition_label)}")

    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"list_edit_{rem_id}")]
        for rem_id, _, _, _ in reminders
    ]), parse_mode="HTML")


@dp.callback_query(F.data.startswith("list_edit_"))
async def list_edit_trigger(callback: CallbackQuery, state: FSMContext):
    rem_id = int(callback.data.split("_", 2)[2])
    reminder = get_reminder_by_id(rem_id)
    if not reminder:
        await callback.message.edit_text("⚠️ Напоминание не найдено.")
        await callback.answer()
        return

    await state.update_data(edit_reminder_id=rem_id)
    user_id, pill_name, time_str, condition = reminder
    condition_label = get_condition_label(condition)
    text = f"✏️ Что менять у лекарства <b>{html_escape(pill_name)}</b>?\n⏰ Сейчас: {time_str}\n🥗 Условие: {html_escape(condition_label)}"
    await callback.message.edit_text(text, reply_markup=get_edit_keyboard(rem_id), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_name_"))
async def edit_name_start(callback: CallbackQuery, state: FSMContext):
    rem_id = int(callback.data.split("_", 2)[2])
    await state.update_data(edit_reminder_id=rem_id)
    await state.set_state(Form.waiting_for_new_name)
    await callback.message.edit_text("Введите новое название лекарства:")
    await callback.answer()


@dp.message(Form.waiting_for_new_name)
async def update_name(message: Message, state: FSMContext):
    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer("❌ Название не может быть пустым.")
        return
    data = await state.get_data()
    rem_id = data.get("edit_reminder_id")
    if rem_id is None:
        await message.answer("❌ Нет активного редактирования.")
        return
    update_reminder(rem_id, pill_name=new_name)
    await state.clear()
    await message.answer(f"✅ Название обновлено на: <b>{html_escape(new_name)}</b>", parse_mode="HTML", reply_markup=get_main_menu())


@dp.callback_query(F.data.startswith("edit_time_"))
async def edit_time_start(callback: CallbackQuery, state: FSMContext):
    rem_id = int(callback.data.split("_", 2)[2])
    await state.update_data(edit_reminder_id=rem_id)
    await state.set_state(Form.waiting_for_new_time)
    await callback.message.edit_text("Введите новое время в формате ЧЧ:ММ:")
    await callback.answer()


@dp.message(Form.waiting_for_new_time)
async def update_time(message: Message, state: FSMContext):
    new_time = (message.text or "").strip()
    try:
        datetime.strptime(new_time, "%H:%M")
    except ValueError:
        await message.answer("❌ Некорректный формат времени. Пример: 08:30")
        return
    data = await state.get_data()
    rem_id = data.get("edit_reminder_id")
    if rem_id is None:
        await message.answer("❌ Нет активного редактирования.")
        return
    update_reminder(rem_id, time_str=new_time)
    reminder = get_reminder_by_id(rem_id)
    if reminder:
        user_id, pill_name, _, _ = reminder
        tz_name = get_user_tz(user_id)
        schedule_reminder(user_id, pill_name, new_time, rem_id, tz_name)
    await state.clear()
    await message.answer(f"✅ Время обновлено на: <b>{new_time}</b>", parse_mode="HTML", reply_markup=get_main_menu())


@dp.callback_query(F.data.startswith("edit_condition_"))
async def edit_condition_start(callback: CallbackQuery, state: FSMContext):
    rem_id = int(callback.data.split("_", 2)[2])
    await state.update_data(edit_reminder_id=rem_id)
    await callback.message.edit_text("Выберите новое условие:", reply_markup=get_condition_keyboard("edit"))
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_schedule_"))
async def edit_schedule_start(callback: CallbackQuery, state: FSMContext):
    rem_id = int(callback.data.split("_", 2)[2])
    await state.update_data(edit_reminder_id=rem_id)
    await callback.message.edit_text(
        "Выберите режим расписания:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🕐 Точное время", callback_data=f"edit_sched_mode_slots_{rem_id}")],
                [InlineKeyboardButton(text="⏱ Интервал в часах", callback_data=f"edit_sched_mode_interval_{rem_id}")],
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_sched_mode_"))
async def edit_schedule_mode(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    rem_id = int(parts[-1])
    mode = parts[3]
    await state.update_data(edit_reminder_id=rem_id, schedule_mode=mode)
    await state.set_state(Form.waiting_for_schedule_count)
    await callback.message.edit_text("Введите сколько раз в день принимать лекарство (1-6):")
    await callback.answer()


@dp.callback_query(F.data.startswith("done_"))
async def action_done(callback: CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    if rem_id in PENDING_MATH_QUIZ:
        PENDING_MATH_QUIZ.pop(rem_id, None)
        if scheduler.get_job(f"quiz_{rem_id}"):
            scheduler.remove_job(f"quiz_{rem_id}")
    data = get_reminder_by_id(rem_id)

    pill_label = f" «{data[1]}»" if data else ""
    now_time = datetime.now().strftime("%H:%M")

    await callback.message.edit_text(f"✅ Вы подтвердили прием лекарства{pill_label} в {now_time}.")
    await callback.answer()


@dp.callback_query(F.data.startswith("delay_"))
async def action_delay(callback: CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    if rem_id in PENDING_MATH_QUIZ:
        PENDING_MATH_QUIZ.pop(rem_id, None)
        if scheduler.get_job(f"quiz_{rem_id}"):
            scheduler.remove_job(f"quiz_{rem_id}")
    data = get_reminder_by_id(rem_id)

    if not data:
        await callback.message.edit_text("⚠️ Ошибка: Напоминание не найдено.")
        await callback.answer()
        return

    user_id, pill_name, _, _ = data
    tz_name = get_user_tz(user_id)
    tz = pytz.timezone(tz_name) if tz_name else pytz.utc
    run_time = datetime.now(tz) + timedelta(minutes=5)

    scheduler.add_job(
        send_pill_reminder,
        "date",
        run_date=run_time,
        args=[user_id, pill_name, rem_id],
        id=f"delay_{rem_id}_{int(run_time.timestamp())}",
        replace_existing=True,
    )

    await callback.message.edit_text("⏰ Напоминание будет повторено через 5 минут.")
    await callback.answer()


@dp.message(F.text)
async def handle_math_answer(message: Message):
    for reminder_id, payload in list(PENDING_MATH_QUIZ.items()):
        if payload.get("user_id") != message.from_user.id:
            continue
        try:
            answer = int((message.text or "").strip())
        except ValueError:
            continue
        if answer == payload.get("answer"):
            payload["solved"] = True
            del PENDING_MATH_QUIZ[reminder_id]
            if scheduler.get_job(f"quiz_{reminder_id}"):
                scheduler.remove_job(f"quiz_{rem_id}")
            await message.answer(
                f"✅ Верно! {payload['pill_name']} принят(а)?",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⏰ Напомнить через 5 минут", callback_data=f"delay_{reminder_id}")],
                        [InlineKeyboardButton(text="✅ Готово", callback_data=f"done_{reminder_id}")],
                    ]
                ),
            )
            break


@dp.callback_query(F.data.startswith("del_"))
async def delete_reminder(callback: CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    delete_reminder_db(rem_id)

    for prefix in ("rem_", "delay_", "quiz_"):
        for job in list(scheduler.get_jobs()):
            if job.id.startswith(f"{prefix}{rem_id}"):
                scheduler.remove_job(job.id)

    await callback.message.edit_text("❌ Напоминание полностью удалено из вашего графика.")
    await callback.answer()


@dp.message(F.text == "🧮 Калькулятор дозировки")
async def dose_calculator_start(message: Message, state: FSMContext):
    await state.set_state(Form.waiting_for_calc_interval)
    await message.answer("Укажите интервал между приемами в часах (например: 8):")


@dp.message(Form.waiting_for_calc_interval)
async def dose_calculator_interval(message: Message, state: FSMContext):
    try:
        interval = float(message.text.strip())
        if interval <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число часов, например 8 или 12.")
        return
    await state.update_data(interval=interval)
    await state.set_state(Form.waiting_for_calc_amount)
    await message.answer("Укажите дозировку в одной таблетке (например: 500):")


@dp.message(Form.waiting_for_calc_amount)
async def dose_calculator_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число дозировки, например 500 или 1.")
        return
    data = await state.get_data()
    interval = float(data.get("interval", 0))
    await state.update_data(amount=amount)
    await state.set_state(Form.waiting_for_calc_start)
    await message.answer(
        f"Укажите время первого приема в формате ЧЧ:ММ (например: 08:00).\n\n" \
        f"Итог: прием каждые {interval} часов, по {amount} единиц(ы) за раз."
    )


@dp.message(Form.waiting_for_calc_start)
async def dose_calculator_start_time(message: Message, state: FSMContext):
    start_time = (message.text or "").strip()
    try:
        start_dt = datetime.strptime(start_time, "%H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат времени. Пример: 08:00")
        return

    data = await state.get_data()
    interval = float(data.get("interval", 0))
    amount = float(data.get("amount", 0)) if "amount" in data else 0.0
    if not interval or not amount:
        await message.answer("❌ Данные калькулятора не найдены, запустите заново.")
        await state.clear()
        return

    slots = []
    for i in range(6):
        dt = start_dt + timedelta(hours=interval * i)
        slots.append(dt.strftime("%H:%M"))

    lines = ["🧮 <b>Калькулятор дозировки</b>", f"📌 Дозировка: {amount}", f"⏱️ Интервал: каждые {interval} часов", "\nРекомендуемая схема:"]
    for i, slot in enumerate(slots, 1):
        lines.append(f"{i}. {slot}")

    await state.clear()
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=get_main_menu())


# ================= TOЧКА ВХОДА =================
async def main():
    init_db()
    scheduler.start()
    restart_all_reminders()
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот выключен.")
