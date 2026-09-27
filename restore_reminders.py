"""Восстановление напоминаний пользователя в базе бота.

Запуск (рядом с базой, на той машине, где работает бот; бота на время остановить):
    python restore_reminders.py <telegram_user_id> [часовой_пояс]

Пример:
    python restore_reminders.py 123456789 Europe/Moscow

Повторный запуск безопасен: уже существующие напоминания не дублируются.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("BOT_TOKEN", "123456:restore-only")  # bot.py требует токен при импорте

import bot  # noqa: E402

# (название, время ЧЧ:ММ, условие: none/before/during/after/empty/night)
REMINDERS = [
    ("Мексидол", "12:00", "none"),
    ("Кораксан", "12:00", "none"),
    ("Фенибут", "12:00", "none"),
    ("милдронат", "16:00", "none"),
]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    user_id = int(sys.argv[1])
    tz_name = sys.argv[2] if len(sys.argv) > 2 else None

    bot.init_db()
    if tz_name:
        bot.set_user_tz(user_id, tz_name)
    elif not bot.get_user_tz(user_id):
        bot.set_user_tz(user_id, "Europe/Moscow")
        print("Часовой пояс не указан — установлен Europe/Moscow")

    existing = {(name, time) for _, name, time, _ in bot.get_user_reminders(user_id)}
    for name, time_str, condition in REMINDERS:
        if (name, time_str) in existing:
            print(f"уже есть: {name} {time_str}")
            continue
        bot.add_reminder(user_id, name, time_str, condition=condition, time_slots=time_str)
        print(f"добавлено: {name} {time_str}")

    print(f"База: {os.path.abspath(bot.DB_NAME)}")
    print("Готово. Запустите бота — напоминания подхватятся автоматически.")


if __name__ == "__main__":
    main()
