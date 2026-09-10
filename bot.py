import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
import pytz

from aiogram import Bot, Dispatcher, F, html
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
TOKEN = os.getenv("BOT_TOKEN", "8958134225:AAHrehK6tYrg1t044_FjWWQSaM2jAfrEWUg") 
DB_NAME = "pill_reminder.db"

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
                time_str TEXT
            )"""
        )
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


def add_reminder(user_id, pill_name, time_str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO reminders (user_id, pill_name, time_str) VALUES (?, ?, ?)",
            (user_id, pill_name, time_str),
        )
        conn.commit()
        return cursor.lastrowid


def get_user_reminders(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, pill_name, time_str FROM reminders WHERE user_id = ?",
            (user_id,),
        )
        return cursor.fetchall()


def get_reminder_by_id(reminder_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, pill_name, time_str FROM reminders WHERE id = ?", (reminder_id,))
        row = cursor.fetchone()
        return row if row else None


def delete_reminder_db(reminder_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()


# ================= СОСТОЯНИЯ (FSM) =================
class Form(StatesGroup):
    waiting_for_pill_name = State()
    waiting_for_time = State()


# ================= КЛАВИАТУРЫ =================
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💊 Добавить лекарство")],
            [KeyboardButton(text="📋 Мои лекарства"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


def get_tz_keyboard():
    buttons = []
    for key, (label, _) in RU_TIMEZONES.items():
        buttons.append([InlineKeyboardButton(text=label, callback_data=key)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ================= ПЛАНИРОВАНИЕ ЗАДАЧ =================
async def send_pill_reminder(user_id: int, pill_name: str, reminder_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM reminders WHERE id = ?", (reminder_id,))
        if not cursor.fetchone():
            return 

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принял(а)", callback_data=f"done_{reminder_id}"),
                InlineKeyboardButton(text="⏰ Через 10 мин", callback_data=f"delay_{reminder_id}"),
            ]
        ]
    )
    try:
        await bot.send_message(
            user_id, f"🔔 Время принять лекарство: <b>{html.escape(pill_name)}</b>!", reply_markup=kb, parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")


def schedule_reminder(user_id, pill_name, time_str, reminder_id, tz_name):
    try:
        user_tz = pytz.timezone(tz_name)
        now_user = datetime.now(user_tz)

        target_time = datetime.strptime(time_str, "%H:%M").time()
        job_datetime = datetime.combine(now_user.date(), target_time)
        job_datetime = user_tz.localize(job_datetime)

        if job_datetime < now_user:
            job_datetime += timedelta(days=1)

        job_id = f"rem_{reminder_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

        utc_datetime = job_datetime.astimezone(pytz.utc)

        scheduler.add_job(
            send_pill_reminder,
            "cron",
            hour=utc_datetime.hour,
            minute=utc_datetime.minute,
            args=[user_id, pill_name, reminder_id],
            id=job_id,
            replace_existing=True,
        )
        logging.info(f"Добавлена задача {job_id} на {time_str} ({tz_name})")
    except Exception as e:
        logging.error(f"Ошибка калибровки времени задачи: {e}")


def restart_all_reminders():
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
    await state.update_data(pill_name=message.text)
    await state.set_state(Form.waiting_for_time)
    await message.answer("Укажите время приема в формате **ЧЧ:ММ** (например: 08:00 или 22:45):")


@dp.message(Form.waiting_for_time)
async def add_pill_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await message.answer("❌ Некорректный формат времени! Попробуйте еще раз. Пример: 07:15 или 20:00")
        return

    user_data = await state.get_data()
    pill_name = user_data["pill_name"]
    user_id = message.from_user.id
    tz_name = get_user_tz(user_id)

    rem_id = add_reminder(user_id, pill_name, time_str)
    schedule_reminder(user_id, pill_name, time_str, rem_id, tz_name)

    await state.clear()
    await message.answer(f"✅ Добавлено регулярное напоминание:\n💊 <b>{html.escape(pill_name)}</b> в ⏰ <b>{time_str}</b>", reply_markup=get_main_menu(), parse_mode="HTML")


@dp.message(F.text == "📋 Мои лекарства")
async def list_reminders(message: Message):
    reminders = get_user_reminders(message.from_user.id)
    if not reminders:
        await message.answer("У вас пока нет активных напоминаний. Нажмите «💊 Добавить лекарство», чтобы создать первое.")
        return

    await message.answer("📋 **Ваш текущий график приема лекарств:**")
    for rem_id, pill_name, time_str in reminders:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🗑 Удалить из базы", callback_data=f"del_{rem_id}")]]
        )
        await message.answer(f"💊 <b>{html.escape(pill_name)}</b>\n⏰ Время: {time_str}", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("del_"))
async def delete_reminder(callback: CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    delete_reminder_db(rem_id)

    job_id = f"rem_{rem_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    await callback.message.edit_text("❌ Напоминание полностью удалено из вашего графика.")
    await callback.answer()


@dp.callback_query(F.data.startswith("done_"))
async def action_done(callback: CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    data = get_reminder_by_id(rem_id)
    
    pill_label = f" «{data[1]}»" if data else ""
    now_time = datetime.now().strftime("%H:%M")
    
    await callback.message.edit_text(f"✅ Вы подтвердили прием лекарства{pill_label} в {now_time}.")
    await callback.answer()


@dp.callback_query(F.data.startswith("delay_"))
async def action_delay(callback: CallbackQuery):
    rem_id = int(callback.data.split("_")[1])
    data = get_reminder_by_id(rem_id)
    
    if not data:
        await callback.message.edit_text("⚠️ Ошибка: Напоминание не найдено.")
        await callback.answer()
        return
        
    user_id, pill_name, _ = data
    run_time = datetime.now() + timedelta(minutes=10)
    
    scheduler.add_job(send_pill_reminder, "date", run_date=run_time, args=[user_id, pill_name, rem_id])

    await callback.message.edit_text("⏰ График изменен. Бот повторно напомнит через 10 минут.")
    await callback.answer()


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
