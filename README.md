# Telegram-магазин одягу

## Файли
- `bot.py` — код бота.
- `requirements.txt` — бібліотеки для встановлення.
- `.env.example` — приклад змінних середовища.

## Налаштування FadeHost
1. Додай у Environment Variables: `BOT_TOKEN` — токен від BotFather; `ADMIN_ID` — твій числовий Telegram ID; `DB_PATH` — `/data/shop.sqlite3`.
2. Runtime/Start command: `python bot.py`.
3. Build/dependencies: встановлення з `requirements.txt`.
4. Переконайся, що сервіс має доступ до постійної папки `/data`.

База SQLite і таблиці створюються автоматично під час запуску. Не додавай справжній токен у GitHub.
