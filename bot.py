import asyncio
import json
import logging
import os
import random
import shutil
import sqlite3
from datetime import datetime, timedelta
from html import escape as html_escape

import pytz
from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

try:
    from aiogram.client.default import DefaultBotProperties
except ImportError:  # aiogram < 3.7
    DefaultBotProperties = None

# ================= НАСТРОЙКА И БЕЗОПАСНОСТЬ =================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it to environment variables or .env")

# Путь к базе. На хостинге укажите путь на постоянном диске (volume), например /data/pill_reminder.db
DB_NAME = os.getenv("DB_NAME", "pill_reminder.db")
LEGACY_DB_NAME = "pill_reminder.db"  # где база лежала раньше — для автопереноса

DELAY_OPTIONS = {15: "15 минут", 30: "30 минут", 60: "1 час"}  # кнопки «Напомнить позже»
QUIZ_REPEAT_MINUTES = 1    # как часто повторять неподтверждённое напоминание
QUIZ_MAX_ATTEMPTS = 30     # после скольких повторов перестать напоминать
PENDING_MAX_AGE_HOURS = 12  # после перезапуска не досылать напоминания старше этого
MAX_PILL_NAME_LEN = 100

logging.basicConfig(level=logging.INFO)
if DefaultBotProperties:
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
else:
    bot = Bot(token=TOKEN, parse_mode="HTML")
scheduler = AsyncIOScheduler(
    timezone=pytz.utc,
    job_defaults={"misfire_grace_time": 300, "coalesce": True},
)

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
    "zone_kamchatka": ("Камчатка (МСК+9 / UTC+12)", "Asia/Kamchatka"),
}

BTN_ADD = "💊 Добавить лекарство"
BTN_LIST = "📋 Мои лекарства"
BTN_SETTINGS = "⚙️ Настройки"
BTN_CALC = "🧮 Калькулятор дозировки"
BTN_CANCEL = "❌ Отмена"
MENU_BUTTONS = {BTN_ADD, BTN_LIST, BTN_SETTINGS, BTN_CALC, BTN_CANCEL}


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


def get_condition_label(value):
    if value is None:
        return "не указано"
    if value in CONDITION_OPTIONS:
        return CONDITION_OPTIONS[value]
    for key, label in CONDITION_OPTIONS.items():
        if value == label:
            return label
    return "не указано"


def prepare_db_file():
    """Создаёт папку под базу и переносит (копирует) старую базу, если путь поменяли."""
    db_path = os.path.abspath(DB_NAME)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        return
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.abspath(LEGACY_DB_NAME), os.path.join(script_dir, LEGACY_DB_NAME)):
        if candidate != db_path and os.path.exists(candidate):
            shutil.copy2(candidate, db_path)
            logging.info(f"Старая база {candidate} скопирована в {db_path}")
            return


def backup_db():
    """Копия базы рядом с ней (<база>.bak) на случай сбоя."""
    if not os.path.exists(DB_NAME):
        return
    try:
        with sqlite3.connect(DB_NAME) as src, sqlite3.connect(DB_NAME + ".bak") as dst:
            src.backup(dst)
    except sqlite3.Error:
        logging.exception("Не удалось сделать резервную копию базы")


def init_db():
    prepare_db_file()
    backup_db()
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
        # Неподтверждённые и отложенные напоминания — чтобы пережить перезапуск бота
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_reminders (
                reminder_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                kind TEXT,
                answer INTEGER,
                attempt INTEGER DEFAULT 0,
                message_id INTEGER,
                next_run TEXT
            )"""
        )
        # Состояния диалогов (FSM)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS fsm_storage (
                key TEXT PRIMARY KEY,
                state TEXT,
                data TEXT
            )"""
        )
        conn.commit()

        # Миграция старых баз: добавляем недостающие колонки, данные не трогаем
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


def log_db_stats():
    with sqlite3.connect(DB_NAME) as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        reminders = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
    logging.info(f"База: {os.path.abspath(DB_NAME)} — пользователей: {users}, напоминаний: {reminders}")


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


def user_now(user_id):
    tz_name = get_user_tz(user_id)
    return datetime.now(pytz.timezone(tz_name) if tz_name else pytz.utc)


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


def get_own_reminder(reminder_id, user_id):
    """Напоминание, только если оно принадлежит пользователю."""
    reminder = get_reminder_by_id(reminder_id)
    if reminder and reminder[0] == user_id:
        return reminder
    return None


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


def delete_reminder_db(reminder_id, user_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
        conn.commit()
        deleted = cursor.rowcount > 0

    if deleted:
        unschedule_reminder(reminder_id)
    return deleted


# --- неподтверждённые / отложенные напоминания ---
# kind: quiz — ждём ответа, будет повтор; expire — последний повтор, потом «не подтверждён»;
#       delay — пользователь отложил напоминание
def save_pending(reminder_id, user_id, kind, next_run, answer=None, attempt=0, message_id=None):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_reminders
                (reminder_id, user_id, kind, answer, attempt, message_id, next_run)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (reminder_id, user_id, kind, answer, attempt, message_id, next_run.isoformat()),
        )
        conn.commit()


def get_pending(reminder_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM pending_reminders WHERE reminder_id = ?", (reminder_id,)).fetchone()
        return dict(row) if row else None


def get_all_pending():
    with sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM pending_reminders").fetchall()]


def get_user_quizzes(user_id):
    """[(reminder_id, правильный ответ)] для вопросов, на которые пользователь ещё не ответил."""
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute(
            "SELECT reminder_id, answer FROM pending_reminders WHERE user_id = ? AND answer IS NOT NULL",
            (user_id,),
        ).fetchall()


def delete_pending(reminder_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM pending_reminders WHERE reminder_id = ?", (reminder_id,))
        conn.commit()


# ================= ХРАНИЛИЩЕ СОСТОЯНИЙ (FSM) =================
class SQLiteStorage(BaseStorage):
    """Состояния диалогов в SQLite: недописанное добавление лекарства переживает перезапуск."""

    @staticmethod
    def _key(key: StorageKey):
        fields = ("bot_id", "chat_id", "user_id", "thread_id", "business_connection_id", "destiny")
        return ":".join(str(getattr(key, field, "") or "") for field in fields)

    def _read(self, key):
        with sqlite3.connect(DB_NAME) as conn:
            row = conn.execute("SELECT state, data FROM fsm_storage WHERE key = ?", (self._key(key),)).fetchone()
        if not row:
            return None, {}
        return row[0], json.loads(row[1] or "{}")

    def _write(self, key, state, data):
        with sqlite3.connect(DB_NAME) as conn:
            if state is None and not data:
                conn.execute("DELETE FROM fsm_storage WHERE key = ?", (self._key(key),))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO fsm_storage (key, state, data) VALUES (?, ?, ?)",
                    (self._key(key), state, json.dumps(data, ensure_ascii=False)),
                )
            conn.commit()

    async def set_state(self, key, state=None):
        _, data = self._read(key)
        self._write(key, state.state if isinstance(state, State) else state, data)

    async def get_state(self, key):
        return self._read(key)[0]

    async def set_data(self, key, data):
        state, _ = self._read(key)
        self._write(key, state, dict(data))

    async def get_data(self, key):
        return self._read(key)[1]

    async def close(self):
        pass


dp = Dispatcher(storage=SQLiteStorage())


# ================= СОСТОЯНИЯ (FSM) =================
class Form(StatesGroup):
    waiting_for_pill_name = State()
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
            [KeyboardButton(text=BTN_ADD)],
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_CALC)],
        ],
        resize_keyboard=True,
    )


def get_cancel_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


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


def get_reminder_keyboard(reminder_id, with_delay=True):
    rows = [[InlineKeyboardButton(text="✅ Готово", callback_data=f"done_{reminder_id}")]]
    if with_delay:
        rows.append(
            [
                InlineKeyboardButton(text=f"⏰ {label}", callback_data=f"delay_{minutes}_{reminder_id}")
                for minutes, label in DELAY_OPTIONS.items()
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_tz_keyboard():
    buttons = []
    for key, (label, _) in RU_TIMEZONES.items():
        buttons.append([InlineKeyboardButton(text=label, callback_data=key)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def parse_callback_id(data):
    """Номер напоминания из конца callback_data (например, edit_name_12 -> 12)."""
    try:
        return int(data.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return None


# ================= ОТПРАВКА СООБЩЕНИЙ =================
async def safe_edit(message, text, reply_markup=None):
    """Редактирует сообщение, не падая на «message is not modified» и удалённых сообщениях."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logging.debug(f"Не удалось изменить сообщение: {e}")


async def safe_delete(chat_id, message_id):
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError as e:
        logging.debug(f"Не удалось удалить сообщение {message_id}: {e}")


async def send_with_retry(chat_id, text, reply_markup=None):
    """Отправка с одним повтором, если Telegram попросил подождать (флуд-контроль)."""
    try:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await bot.send_message(chat_id, text, reply_markup=reply_markup)


# ================= ПЛАНИРОВАНИЕ ЗАДАЧ =================
def normalize_schedule_reminder(reminder_id):
    """Список времён приёма ЧЧ:ММ для напоминания."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT time_str, schedule_mode, times_per_day, interval_hours, first_time, time_slots FROM reminders WHERE id = ?",
            (reminder_id,),
        )
        row = cursor.fetchone()
    if not row:
        return []

    time_str, schedule_mode, times_per_day, interval_hours, first_time, time_slots = row
    times = []
    mode = (schedule_mode or "slots").strip() or "slots"
    try:
        count = int(times_per_day or 1)
        interval = float(interval_hours or 0)
    except (TypeError, ValueError):
        count, interval = 1, 0
    if mode == "interval":
        start_time = first_time or time_str
        try:
            start_dt = datetime.strptime(start_time, "%H:%M") if start_time else None
        except ValueError:
            start_dt = None
        if start_dt and interval > 0:
            for i in range(count):
                times.append((start_dt + timedelta(hours=interval * i)).strftime("%H:%M"))
        elif time_str:
            times.append(time_str)
    else:
        raw_slots = time_slots or time_str or ""
        for item in str(raw_slots).split(","):
            value = item.strip()
            if value:
                try:
                    times.append(datetime.strptime(value, "%H:%M").strftime("%H:%M"))
                except ValueError:
                    continue
    # убираем дубликаты, сохраняя порядок
    return list(dict.fromkeys(times))


def cancel_pending_job(reminder_id):
    try:
        scheduler.remove_job(f"pending_{reminder_id}")
    except JobLookupError:
        pass


def clear_pending(reminder_id):
    """Останавливает повторы/отложенное напоминание и забывает вопрос."""
    cancel_pending_job(reminder_id)
    delete_pending(reminder_id)


def schedule_pending_job(reminder_id, kind, run_date, attempt=0):
    """Одна задача на напоминание: следующий повтор, финальная отметка или отложенный приём."""
    if kind == "quiz":
        func, args = send_smart_reminder, [reminder_id, attempt + 1]
    elif kind == "expire":
        func, args = expire_reminder, [reminder_id]
    else:  # delay
        func, args = send_smart_reminder, [reminder_id]
    scheduler.add_job(
        func,
        "date",
        run_date=run_date,
        args=args,
        id=f"pending_{reminder_id}",
        replace_existing=True,
    )


def unschedule_reminder(reminder_id, include_pending=True):
    """Удаляет задачи напоминания. Точное сравнение префиксов, чтобы rem_1 не задевал rem_12."""
    for job in list(scheduler.get_jobs()):
        if job.id.startswith(f"rem_{reminder_id}_"):
            scheduler.remove_job(job.id)
    if include_pending:
        clear_pending(reminder_id)


async def expire_reminder(reminder_id):
    """Серия повторов закончилась без ответа: помечаем сообщение, оставляем только «Готово»."""
    pending = get_pending(reminder_id)
    clear_pending(reminder_id)
    if not pending or not pending.get("message_id"):
        return
    reminder = get_reminder_by_id(reminder_id)
    pill_name = html_escape(reminder[1]) if reminder else "лекарства"
    try:
        await bot.edit_message_text(
            f"⚠️ Приём <b>{pill_name}</b> не подтверждён.\nЕсли вы уже приняли лекарство, нажмите «Готово».",
            chat_id=pending["user_id"],
            message_id=pending["message_id"],
            reply_markup=get_reminder_keyboard(reminder_id, with_delay=False) if reminder else None,
        )
    except TelegramAPIError as e:
        logging.debug(f"Не удалось пометить напоминание {reminder_id}: {e}")


async def send_smart_reminder(reminder_id: int, attempt: int = 1):
    reminder = get_reminder_by_id(reminder_id)
    if not reminder:
        clear_pending(reminder_id)
        return
    user_id, pill_name, _, condition = reminder

    # старое сообщение с вопросом убираем, чтобы в чате было одно актуальное
    previous = get_pending(reminder_id)
    if previous and previous.get("message_id"):
        await safe_delete(user_id, previous["message_id"])

    condition_text = get_condition_label(condition)
    a = random.randint(1, 5)
    b = random.randint(1, 5)
    message_id = None
    try:
        sent = await send_with_retry(
            user_id,
            f"🔔 Время принять лекарство: <b>{html_escape(pill_name)}</b>\n"
            f"📌 Условие: <b>{html_escape(condition_text)}</b>\n\n"
            f"🧠 Быстрая проверка: <b>{a} + {b}</b> = ?\n"
            "Напишите ответ цифрой или нажмите «Готово». Отложить можно кнопками ниже.",
            reply_markup=get_reminder_keyboard(reminder_id),
        )
        message_id = sent.message_id
    except TelegramForbiddenError:
        # пользователь заблокировал бота — не повторяем; данные не трогаем
        logging.warning(f"Пользователь {user_id} заблокировал бота, повторы напоминания {reminder_id} остановлены")
        clear_pending(reminder_id)
        return
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

    # Повторяем, пока пользователь не подтвердит, но не бесконечно
    next_run = datetime.now(pytz.utc) + timedelta(minutes=QUIZ_REPEAT_MINUTES)
    kind = "quiz" if attempt < QUIZ_MAX_ATTEMPTS else "expire"
    save_pending(reminder_id, user_id, kind, next_run, answer=a + b, attempt=attempt, message_id=message_id)
    schedule_pending_job(reminder_id, kind, next_run, attempt)


async def send_pill_reminder(reminder_id: int):
    # новый приём закрывает прошлую неподтверждённую серию
    if get_pending(reminder_id):
        await expire_reminder(reminder_id)
    await send_smart_reminder(reminder_id)


def schedule_reminder(reminder_id, tz_name):
    """Планирует все времена приёма напоминания. Возвращает True при успехе."""
    try:
        if not tz_name:
            logging.warning(f"Не удалось запланировать напоминание {reminder_id}: не задан часовой пояс")
            return False

        # убираем старые слоты (их могло быть больше, чем сейчас)
        unschedule_reminder(reminder_id, include_pending=False)

        slots = normalize_schedule_reminder(reminder_id)
        if not slots:
            logging.warning(f"У напоминания {reminder_id} нет корректных времён приёма")
            return False

        user_tz = pytz.timezone(tz_name)
        for idx, slot in enumerate(slots):
            target_time = datetime.strptime(slot, "%H:%M").time()
            job_id = f"rem_{reminder_id}_{idx}"
            scheduler.add_job(
                send_pill_reminder,
                "cron",
                hour=target_time.hour,
                minute=target_time.minute,
                second=0,
                timezone=user_tz,
                args=[reminder_id],
                id=job_id,
                replace_existing=True,
            )
            logging.info(f"Добавлена задача {job_id} на {slot} ({tz_name})")
        return True
    except Exception:
        logging.exception(f"Ошибка планирования напоминания {reminder_id}")
        return False


def reschedule_user_reminders(user_id, tz_name):
    for rem_id, _, _, _ in get_user_reminders(user_id):
        schedule_reminder(rem_id, tz_name)


def restart_all_reminders():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT r.id, u.timezone
            FROM reminders r
            JOIN users u ON r.user_id = u.user_id
        """
        )
        rows = cursor.fetchall()

    try:
        scheduler.remove_all_jobs()
    except Exception as e:
        logging.warning(f"Не удалось удалить старые job'ы перед восстановлением: {e}")

    for rem_id, tz in rows:
        schedule_reminder(rem_id, tz)
    logging.info(f"Успешно восстановлено задач из базы: {len(rows)}")


def restore_pending():
    """После перезапуска продолжает повторы и отложенные напоминания."""
    now = datetime.now(pytz.utc)
    restored = 0
    for row in get_all_pending():
        rem_id = row["reminder_id"]
        try:
            run_date = datetime.fromisoformat(row["next_run"])
        except (TypeError, ValueError):
            delete_pending(rem_id)
            continue
        if not get_reminder_by_id(rem_id) or now - run_date > timedelta(hours=PENDING_MAX_AGE_HOURS):
            delete_pending(rem_id)
            continue
        schedule_pending_job(rem_id, row["kind"], max(run_date, now + timedelta(seconds=5)), row["attempt"] or 0)
        restored += 1
    logging.info(f"Восстановлено неподтверждённых/отложенных напоминаний: {restored}")


def apply_schedule_change(reminder_id, **fields):
    """Сохраняет расписание, обновляет time_str на первое время и перепланирует."""
    update_reminder(reminder_id, **fields)
    slots = normalize_schedule_reminder(reminder_id)
    if slots:
        update_reminder(reminder_id, time_str=slots[0])
    reminder = get_reminder_by_id(reminder_id)
    if reminder:
        schedule_reminder(reminder_id, get_user_tz(reminder[0]))
    return slots


# ================= ОБРАБОТЧИКИ: КОМАНДЫ И МЕНЮ =================
# Кнопки меню зарегистрированы первыми, чтобы работать из любого состояния.
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я надежный бот-напоминалка о приеме лекарств. 💊\n\n"
        "Пожалуйста, выберите ваш <b>часовой пояс</b> для точной отправки уведомлений:",
        reply_markup=get_tz_keyboard(),
    )


@dp.message(Command("cancel"))
@dp.message(F.text == BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_menu())


@dp.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Выберите ваш актуальный часовой пояс России:", reply_markup=get_tz_keyboard())


@dp.message(F.text == BTN_ADD)
async def add_pill_start(message: Message, state: FSMContext):
    await state.clear()  # сбрасываем остатки прошлого редактирования
    if not get_user_tz(message.from_user.id):
        await message.answer("⚠️ Сначала укажите ваш часовой пояс в разделе ⚙️ Настройки")
        return
    await state.set_state(Form.waiting_for_pill_name)
    await message.answer(
        "Введите точное название лекарства (например: Ибупрофен):", reply_markup=get_cancel_menu()
    )


@dp.message(F.text == BTN_LIST)
async def list_reminders(message: Message, state: FSMContext):
    await state.clear()
    reminders = get_user_reminders(message.from_user.id)
    if not reminders:
        await message.answer(
            "У вас пока нет активных напоминаний. Нажмите «💊 Добавить лекарство», чтобы создать первое.",
            reply_markup=get_main_menu(),
        )
        return

    tz_name = get_user_tz(message.from_user.id)
    lines = ["📋 <b>Ваш текущий график приема лекарств</b>", f"⏱️ Часовой пояс: {get_tz_label(tz_name)}"]
    buttons = []
    for rem_id, pill_name, time_str, condition in reminders:
        times = ", ".join(normalize_schedule_reminder(rem_id)) or time_str
        lines.append(
            f"\n💊 <b>{html_escape(pill_name)}</b> — ⏰ {times} — 🥗 {html_escape(get_condition_label(condition))}"
        )
        short_name = pill_name if len(pill_name) <= 25 else pill_name[:24] + "…"
        buttons.append([InlineKeyboardButton(text=f"✏️ {short_name}", callback_data=f"list_edit_{rem_id}")])

    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.message(F.text == BTN_CALC)
async def dose_calculator_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Form.waiting_for_calc_interval)
    await message.answer("Укажите интервал между приемами в часах (например: 8):", reply_markup=get_cancel_menu())


# ================= ОБРАБОТЧИКИ: КНОПКИ (CALLBACK) =================
@dp.callback_query(F.data.startswith("zone_"))
async def select_timezone(callback: CallbackQuery):
    tz_code = callback.data
    if tz_code not in RU_TIMEZONES:
        await callback.answer("Неизвестный часовой пояс", show_alert=True)
        return
    label, tz_name = RU_TIMEZONES[tz_code]
    user_id = callback.from_user.id
    set_user_tz(user_id, tz_name)
    reschedule_user_reminders(user_id, tz_name)

    await safe_edit(callback.message, f"✅ Успешно установлен часовой пояс:\n<b>{label}</b>")
    await callback.message.answer(
        "Вы можете добавлять лекарства и настраивать график через меню ниже.", reply_markup=get_main_menu()
    )
    await callback.answer()


@dp.callback_query(StateFilter(Form.waiting_for_schedule_mode), F.data.startswith("schedule_mode_"))
async def set_schedule_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.replace("schedule_mode_", "")
    if mode not in SCHEDULE_MODES:
        await callback.answer()
        return
    await state.update_data(schedule_mode=mode)
    await state.set_state(Form.waiting_for_schedule_count)
    await callback.message.edit_text("Сколько раз в день принимать? Введите число от 1 до 6:")
    await callback.answer()


@dp.callback_query(F.data.startswith("schedule_mode_"))
async def stale_schedule_mode(callback: CallbackQuery):
    await callback.answer("Эта кнопка устарела. Начните добавление заново.", show_alert=True)


@dp.callback_query(F.data.startswith("cond_"))
async def handle_condition_choice(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    if len(parts) < 3 or parts[2] not in CONDITION_OPTIONS:
        await callback.answer("⚠️ Не удалось обработать условие.", show_alert=True)
        return

    action = parts[1]
    condition_key = parts[2]
    condition_label = get_condition_label(condition_key)
    current_state = await state.get_state()
    user_id = callback.from_user.id

    try:
        if action == "add" and current_state == Form.waiting_for_condition.state:
            user_data = await state.get_data()
            tz_name = get_user_tz(user_id)
            pill_name = str(user_data.get("pill_name", "")).strip()
            mode = user_data.get("schedule_mode", "slots")
            slots = user_data.get("slots") or []

            if not pill_name or not slots:
                await state.clear()
                await callback.message.edit_text("⚠️ Данные напоминания потеряны. Попробуйте добавить лекарство заново.")
                await callback.answer()
                return

            rem_id = add_reminder(
                user_id,
                pill_name,
                slots[0],
                condition=condition_key,
                schedule_mode=mode,
                times_per_day=int(user_data.get("times_per_day", 1) or 1),
                interval_hours=float(user_data.get("interval_hours", 0) or 0),
                first_time=str(user_data.get("first_time", "") or ""),
                time_slots=str(user_data.get("time_slots", "") or ""),
            )
            await state.clear()

            if not schedule_reminder(rem_id, tz_name):
                delete_reminder_db(rem_id, user_id)
                await callback.message.edit_text("⚠️ Не удалось создать напоминание. Попробуйте ещё раз.")
                await callback.message.answer("Главное меню", reply_markup=get_main_menu())
                await callback.answer()
                return

            summary = (
                "✅ Напоминание добавлено\n"
                f"💊 <b>{html_escape(pill_name)}</b>\n"
                f"⏰ <b>{', '.join(slots)}</b>\n"
                f"🥗 <b>{html_escape(condition_label)}</b>\n"
                f"🕒 {get_tz_label(tz_name)}"
            )
            await callback.answer()
            await callback.message.edit_text(summary)
            await callback.message.answer("Главное меню", reply_markup=get_main_menu())
            return

        if action == "edit" and current_state == Form.waiting_for_new_condition.state:
            edit_id = (await state.get_data()).get("edit_reminder_id")
            await state.clear()
            if edit_id is None or not get_own_reminder(edit_id, user_id):
                await callback.message.edit_text("⚠️ Напоминание не найдено.")
                await callback.answer()
                return

            update_reminder(edit_id, condition=condition_key)
            await callback.answer()
            await callback.message.edit_text(f"✅ Условие обновлено: <b>{html_escape(condition_label)}</b>")
            await callback.message.answer("Главное меню", reply_markup=get_main_menu())
            return

        await callback.answer("Эта кнопка устарела.", show_alert=True)
    except Exception:
        logging.exception("Ошибка при обработке выбора условия")
        await callback.answer("⚠️ Ошибка при сохранении условия.", show_alert=True)


@dp.callback_query(F.data.startswith("list_edit_"))
async def list_edit_trigger(callback: CallbackQuery, state: FSMContext):
    rem_id = parse_callback_id(callback.data)
    reminder = get_own_reminder(rem_id, callback.from_user.id) if rem_id else None
    if not reminder:
        await callback.message.edit_text("⚠️ Напоминание не найдено.")
        await callback.answer()
        return

    await state.clear()
    await state.update_data(edit_reminder_id=rem_id)
    _, pill_name, time_str, condition = reminder
    times = ", ".join(normalize_schedule_reminder(rem_id)) or time_str
    text = (
        f"✏️ Что менять у лекарства <b>{html_escape(pill_name)}</b>?\n"
        f"⏰ Сейчас: {times}\n"
        f"🥗 Условие: {html_escape(get_condition_label(condition))}"
    )
    await callback.message.edit_text(text, reply_markup=get_edit_keyboard(rem_id))
    await callback.answer()


async def start_edit(callback: CallbackQuery, state: FSMContext):
    """Общая проверка для кнопок редактирования. Возвращает id или None."""
    rem_id = parse_callback_id(callback.data)
    if not rem_id or not get_own_reminder(rem_id, callback.from_user.id):
        await callback.message.edit_text("⚠️ Напоминание не найдено.")
        await callback.answer()
        return None
    await state.clear()
    await state.update_data(edit_reminder_id=rem_id)
    return rem_id


@dp.callback_query(F.data.startswith("edit_name_"))
async def edit_name_start(callback: CallbackQuery, state: FSMContext):
    if await start_edit(callback, state) is None:
        return
    await state.set_state(Form.waiting_for_new_name)
    await callback.message.edit_text("Введите новое название лекарства:")
    await callback.message.answer("Или нажмите «Отмена».", reply_markup=get_cancel_menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_time_"))
async def edit_time_start(callback: CallbackQuery, state: FSMContext):
    if await start_edit(callback, state) is None:
        return
    await state.set_state(Form.waiting_for_new_time)
    await callback.message.edit_text(
        "Введите новое время в формате ЧЧ:ММ.\n"
        "Будет установлен один прием в день. Для нескольких приемов используйте «🕒 Расписание»."
    )
    await callback.message.answer("Или нажмите «Отмена».", reply_markup=get_cancel_menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_condition_"))
async def edit_condition_start(callback: CallbackQuery, state: FSMContext):
    if await start_edit(callback, state) is None:
        return
    await state.set_state(Form.waiting_for_new_condition)
    await callback.message.edit_text("Выберите новое условие:", reply_markup=get_condition_keyboard("edit"))
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_schedule_"))
async def edit_schedule_start(callback: CallbackQuery, state: FSMContext):
    rem_id = await start_edit(callback, state)
    if rem_id is None:
        return
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
    mode = parts[3] if len(parts) == 5 else None
    if mode not in SCHEDULE_MODES:
        await callback.answer()
        return
    if await start_edit(callback, state) is None:
        return
    await state.update_data(schedule_mode=mode)
    await state.set_state(Form.waiting_for_schedule_count)
    await callback.message.edit_text("Введите сколько раз в день принимать лекарство (1-6):")
    await callback.message.answer("Или нажмите «Отмена».", reply_markup=get_cancel_menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("done_"))
async def action_done(callback: CallbackQuery):
    rem_id = parse_callback_id(callback.data)
    data = get_own_reminder(rem_id, callback.from_user.id) if rem_id else None
    if data:
        # останавливаем и повторы, и отложенное напоминание
        pending = get_pending(rem_id)
        clear_pending(rem_id)
        if pending and pending.get("message_id") and pending["message_id"] != callback.message.message_id:
            await safe_delete(callback.from_user.id, pending["message_id"])

    pill_label = f" «{html_escape(data[1])}»" if data else ""
    now_time = user_now(callback.from_user.id).strftime("%H:%M")

    await safe_edit(callback.message, f"✅ Вы подтвердили прием лекарства{pill_label} в {now_time}.")
    await callback.answer()


@dp.callback_query(F.data.startswith("delay_"))
async def action_delay(callback: CallbackQuery):
    # delay_<минуты>_<id>; старые кнопки из чата — delay_<id>
    parts = callback.data.split("_")
    rem_id = parse_callback_id(callback.data)
    minutes = next(iter(DELAY_OPTIONS))
    if len(parts) == 3:
        try:
            minutes = int(parts[1])
        except ValueError:
            minutes = None
    if minutes not in DELAY_OPTIONS:
        await callback.answer("Эта кнопка устарела.", show_alert=True)
        return

    data = get_own_reminder(rem_id, callback.from_user.id) if rem_id else None
    if not data:
        await safe_edit(callback.message, "⚠️ Ошибка: Напоминание не найдено.")
        await callback.answer()
        return

    pending = get_pending(rem_id)
    if pending and pending.get("message_id") and pending["message_id"] != callback.message.message_id:
        await safe_delete(callback.from_user.id, pending["message_id"])

    run_time = datetime.now(pytz.utc) + timedelta(minutes=minutes)
    cancel_pending_job(rem_id)
    save_pending(rem_id, callback.from_user.id, "delay", run_time)
    schedule_pending_job(rem_id, "delay", run_time)

    await safe_edit(
        callback.message,
        f"⏰ Напомню о <b>{html_escape(data[1])}</b> через {DELAY_OPTIONS[minutes]}.",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("del_"))
async def delete_reminder(callback: CallbackQuery, state: FSMContext):
    rem_id = parse_callback_id(callback.data)
    await state.clear()
    if rem_id and delete_reminder_db(rem_id, callback.from_user.id):
        await safe_edit(callback.message, "❌ Напоминание полностью удалено из вашего графика.")
    else:
        await safe_edit(callback.message, "⚠️ Напоминание не найдено.")
    await callback.answer()


# ================= ОБРАБОТЧИКИ: ВВОД ТЕКСТА ПО СОСТОЯНИЯМ =================
@dp.message(Form.waiting_for_pill_name)
async def add_pill_name(message: Message, state: FSMContext):
    pill_name = (message.text or "").strip()
    if not pill_name:
        await message.answer("❌ Название лекарства не может быть пустым. Введите корректное название.")
        return
    if len(pill_name) > MAX_PILL_NAME_LEN:
        await message.answer(f"❌ Слишком длинное название (максимум {MAX_PILL_NAME_LEN} символов).")
        return

    await state.update_data(pill_name=pill_name)
    await state.set_state(Form.waiting_for_schedule_mode)
    await message.answer("Как хотели бы задавать приемы?", reply_markup=get_schedule_mode_keyboard())


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
        example = ", ".join(["08:00", "13:00", "19:00", "22:00", "10:00", "16:00"][:count])
        await message.answer(f"Введите {count} времени(ен) через запятую, например: {example}")
    else:
        await state.set_state(Form.waiting_for_interval_value)
        await message.answer("Введите первый прием и шаг в часах через пробел, например: 08:00 6")


async def finish_schedule_input(message: Message, state: FSMContext, **fields):
    """После ввода расписания: при редактировании сохраняем, при добавлении — спрашиваем условие."""
    data = await state.get_data()
    edit_id = data.get("edit_reminder_id")
    if edit_id is not None:
        await state.clear()
        if not get_own_reminder(edit_id, message.from_user.id):
            await message.answer("⚠️ Напоминание не найдено.", reply_markup=get_main_menu())
            return
        db_fields = {k: v for k, v in fields.items() if k != "slots"}
        slots = apply_schedule_change(edit_id, **db_fields)
        await message.answer(f"✅ Расписание обновлено: {', '.join(slots)}", reply_markup=get_main_menu())
        return

    await state.update_data(**fields)
    await state.set_state(Form.waiting_for_condition)
    await message.answer("Выберите, когда принимать лекарство:", reply_markup=get_condition_keyboard("add"))


@dp.message(Form.waiting_for_slot_times)
async def set_slot_times(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    slots = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            slots.append(datetime.strptime(value, "%H:%M").strftime("%H:%M"))
        except ValueError:
            await message.answer("❌ В одном из времен ошибка. Используйте формат ЧЧ:ММ, например: 08:00, 13:30")
            return

    slots = list(dict.fromkeys(slots))
    data = await state.get_data()
    count = int(data.get("times_per_day", 1))
    if len(slots) != count:
        await message.answer(f"❌ Нужно указать ровно {count} разных времени(ен) через запятую.")
        return

    await finish_schedule_input(
        message,
        state,
        schedule_mode="slots",
        times_per_day=count,
        time_slots=", ".join(slots),
        slots=slots,
    )


@dp.message(Form.waiting_for_interval_value)
async def set_interval_value(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        parts = raw.split()
        if len(parts) != 2:
            raise ValueError
        first_time = datetime.strptime(parts[0], "%H:%M").strftime("%H:%M")
        interval = float(parts[1].replace(",", "."))
        if interval <= 0 or interval > 24:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: 08:00 6 (шаг от 0 до 24 часов)")
        return

    data = await state.get_data()
    count = int(data.get("times_per_day", 1))
    start_dt = datetime.strptime(first_time, "%H:%M")
    slots = list(dict.fromkeys(
        (start_dt + timedelta(hours=interval * i)).strftime("%H:%M") for i in range(count)
    ))
    if len(slots) != count:
        await message.answer("❌ При таком шаге времена приёма совпадают. Уменьшите шаг или число приёмов.")
        return

    await finish_schedule_input(
        message,
        state,
        schedule_mode="interval",
        times_per_day=count,
        first_time=first_time,
        interval_hours=interval,
        slots=slots,
    )


@dp.message(Form.waiting_for_new_name)
async def update_name(message: Message, state: FSMContext):
    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer("❌ Название не может быть пустым.")
        return
    if len(new_name) > MAX_PILL_NAME_LEN:
        await message.answer(f"❌ Слишком длинное название (максимум {MAX_PILL_NAME_LEN} символов).")
        return
    rem_id = (await state.get_data()).get("edit_reminder_id")
    await state.clear()
    if rem_id is None or not get_own_reminder(rem_id, message.from_user.id):
        await message.answer("❌ Нет активного редактирования.", reply_markup=get_main_menu())
        return
    update_reminder(rem_id, pill_name=new_name)
    await message.answer(f"✅ Название обновлено на: <b>{html_escape(new_name)}</b>", reply_markup=get_main_menu())


@dp.message(Form.waiting_for_new_time)
async def update_time(message: Message, state: FSMContext):
    try:
        new_time = datetime.strptime((message.text or "").strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        await message.answer("❌ Некорректный формат времени. Пример: 08:30")
        return
    rem_id = (await state.get_data()).get("edit_reminder_id")
    await state.clear()
    if rem_id is None or not get_own_reminder(rem_id, message.from_user.id):
        await message.answer("❌ Нет активного редактирования.", reply_markup=get_main_menu())
        return
    # Время хранится в time_slots/first_time, поэтому меняем расписание целиком
    apply_schedule_change(rem_id, schedule_mode="slots", times_per_day=1, time_slots=new_time)
    await message.answer(f"✅ Время обновлено на: <b>{new_time}</b>", reply_markup=get_main_menu())


@dp.message(Form.waiting_for_calc_interval)
async def dose_calculator_interval(message: Message, state: FSMContext):
    try:
        interval = float((message.text or "").strip().replace(",", "."))
        if interval <= 0 or interval > 24:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число часов от 0 до 24, например 8 или 12.")
        return
    await state.update_data(interval=interval)
    await state.set_state(Form.waiting_for_calc_amount)
    await message.answer("Укажите дозировку в одной таблетке (например: 500):")


@dp.message(Form.waiting_for_calc_amount)
async def dose_calculator_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").strip().replace(",", "."))
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
        "Укажите время первого приема в формате ЧЧ:ММ (например: 08:00).\n\n"
        f"Итог: прием каждые {interval:g} ч, по {amount:g} единиц(ы) за раз."
    )


@dp.message(Form.waiting_for_calc_start)
async def dose_calculator_start_time(message: Message, state: FSMContext):
    try:
        start_dt = datetime.strptime((message.text or "").strip(), "%H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат времени. Пример: 08:00")
        return

    data = await state.get_data()
    interval = float(data.get("interval", 0))
    amount = float(data.get("amount", 0))
    await state.clear()
    if not interval or not amount:
        await message.answer("❌ Данные калькулятора не найдены, запустите заново.", reply_markup=get_main_menu())
        return

    # Приёмы в пределах одних суток
    doses = max(1, int(24 // interval))
    slots = [(start_dt + timedelta(hours=interval * i)).strftime("%H:%M") for i in range(doses)]

    lines = [
        "🧮 <b>Калькулятор дозировки</b>",
        f"📌 Дозировка за прием: {amount:g}",
        f"⏱️ Интервал: каждые {interval:g} ч",
        f"📊 В сутки: {doses} прием(а), всего {amount * doses:g}",
        "\nРекомендуемая схема:",
    ]
    for i, slot in enumerate(slots, 1):
        lines.append(f"{i}. {slot}")

    await message.answer("\n".join(lines), reply_markup=get_main_menu())


# ================= ОТВЕТ НА ПРОВЕРКУ (последним, только вне состояний) =================
@dp.message(StateFilter(None), F.text)
async def handle_math_answer(message: Message):
    try:
        answer = int((message.text or "").strip())
    except ValueError:
        return

    user_quizzes = get_user_quizzes(message.from_user.id)
    if not user_quizzes:
        return

    for reminder_id, expected in user_quizzes:
        if answer == expected:
            clear_pending(reminder_id)
            reminder = get_reminder_by_id(reminder_id)
            pill_name = html_escape(reminder[1]) if reminder else "Лекарство"
            await message.answer(
                f"✅ Верно! <b>{pill_name}</b> принят(а)?",
                reply_markup=get_reminder_keyboard(reminder_id),
            )
            return

    await message.answer("❌ Неверно, попробуйте ещё раз.")


# ================= TOЧКА ВХОДА =================
async def main():
    init_db()
    log_db_stats()
    scheduler.start()
    restart_all_reminders()
    restore_pending()
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот выключен.")
