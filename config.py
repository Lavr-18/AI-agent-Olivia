import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "7841980969:AAFalwN9tZ5LHaPc4bAsKm5wHb_LS3DfbMA")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
RETAIL_CRM = os.getenv("RETAIL_CRM", "Ffv4T1XSozPcTqsqzAsetWGrsfjEwWXP")
MOY_SKLAD = os.getenv("MOY_SKLAD", "34a594fc3d727f72aa20883d6a2dc4f3e41844b4")
RETAIL_CRM_BOT_TOKEN = os.getenv("RETAIL_CRM_BOT_TOKEN", "296073438a5a05701d83c6cfa72093f6772898e985a5520b552f1b041fd79e021f5ab")

API_URL = "https://mg-s1.retailcrm.pro/api/bot/v1"
RETAILCRM_BASE_URL = "https://tropichouse.retailcrm.ru"

# MG API конфигурация
MG_URL = "https://mg-s1.retailcrm.pro/api/bot/v1"
MG_TOKEN = RETAIL_CRM_BOT_TOKEN
MG_HEADERS = MG_TOKEN

# Группы менеджеров
MANAGER_B2B = {"symbol": "manager b2b", "id": 71, "group": "b2b"}
MANAGER_B2C = {"symbol": "manager", "id": 2, "group": "b2c"}

# Telegram Chat/Topic Configuration
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1003056310422")
TELEGRAM_TOPIC_ID = os.getenv("TELEGRAM_TOPIC_ID", "6")