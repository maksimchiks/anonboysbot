import os
import sys

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ ОШИБКА: Переменная окружения BOT_TOKEN не установлена!")
    print("Установите её перед запуском бота.")
    sys.exit(1)

MOD_LOG_CHAT_ID = -1003832839174  # ID канала/чата для логов
MAX_REPORTS = 50
ADMINS = [
    8484827434,  # ТВОЙ REAL TG ID, БЕЗ КАВЫЧЕК
]


